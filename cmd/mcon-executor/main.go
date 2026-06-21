package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
)

type ExecSpec struct {
	Argv    []string          `json:"argv"`
	Cwd     string            `json:"cwd"`
	Env     map[string]string `json:"env"`
	Stdin   string            `json:"stdin"`
	Sandbox SandboxSpec       `json:"sandbox"`
}

type SandboxSpec struct {
	Enabled      bool       `json:"enabled"`
	Workspace    string     `json:"workspace"`
	Network      string     `json:"network"`
	SessionID    string     `json:"session_id"`
	SessionRoot  string     `json:"session_root"`
	Files        []FileRule `json:"files"`
	RuntimeFiles []FileRule `json:"runtime_files"`
}

type FileRule struct {
	Action string `json:"action"`
	Path   string `json:"path"`
}

func main() {
	if err := run(os.Stdin, os.Stdout, os.Stderr); err != nil {
		fmt.Fprintf(os.Stderr, "mcon-executor: %v\n", err)
		os.Exit(1)
	}
}

func run(stdin io.Reader, stdout io.Writer, stderr io.Writer) error {
	var spec ExecSpec
	decoder := json.NewDecoder(stdin)
	if err := decoder.Decode(&spec); err != nil {
		return fmt.Errorf("decode exec spec: %w", err)
	}
	if len(spec.Argv) == 0 || spec.Argv[0] == "" {
		return errors.New("argv must contain a command")
	}

	command, err := buildCommand(spec)
	if err != nil {
		return err
	}
	if spec.Stdin != "" {
		command.Stdin = strings.NewReader(spec.Stdin)
	}
	command.Stdout = stdout
	command.Stderr = stderr
	if err := command.Run(); err != nil {
		return err
	}
	if spec.Sandbox.Enabled {
		if err := applySessionWrites(spec); err != nil {
			return err
		}
	}
	return nil
}

func buildCommand(spec ExecSpec) (*exec.Cmd, error) {
	if len(spec.Argv) == 0 || spec.Argv[0] == "" {
		return nil, errors.New("argv must contain a command")
	}
	env := buildEnv(spec.Env)
	if !spec.Sandbox.Enabled {
		command := exec.Command(spec.Argv[0], spec.Argv[1:]...)
		if spec.Cwd != "" {
			command.Dir = spec.Cwd
		}
		command.Env = env
		return command, nil
	}

	workspace := spec.Sandbox.Workspace
	if workspace == "" {
		workspace = "/workspace"
	}
	cleanWorkspace, err := cleanAbsolutePath(workspace)
	if err != nil {
		return nil, fmt.Errorf("invalid workspace: %w", err)
	}
	cwd := spec.Cwd
	if cwd == "" {
		cwd = cleanWorkspace
	}
	cleanCwd, err := cleanAbsolutePath(cwd)
	if err != nil {
		return nil, fmt.Errorf("invalid cwd: %w", err)
	}
	if !pathWithin(cleanWorkspace, cleanCwd) {
		return nil, fmt.Errorf("cwd must be inside workspace: %s", cleanCwd)
	}

	args, err := buildBubblewrapArgs(spec, cleanWorkspace, cleanCwd)
	if err != nil {
		return nil, err
	}
	command := exec.Command("bwrap", args...)
	command.Env = env
	return command, nil
}

func buildEnv(env map[string]string) []string {
	if env == nil {
		env = map[string]string{}
	}
	if _, ok := env["PATH"]; !ok {
		env["PATH"] = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
	}
	result := make([]string, 0, len(env))
	for key, value := range env {
		result = append(result, key+"="+value)
	}
	return result
}

func buildBubblewrapArgs(spec ExecSpec, workspace string, cwd string) ([]string, error) {
	networkMode := spec.Sandbox.Network
	if networkMode == "" {
		networkMode = "none"
	}
	if networkMode != "none" && networkMode != "inherit" {
		return nil, fmt.Errorf("unsupported sandbox network mode: %s", networkMode)
	}
	sessionRoot := spec.Sandbox.SessionRoot
	if sessionRoot == "" {
		sessionRoot = "/mcon/session-fs"
	}
	cleanSessionRoot, err := cleanAbsolutePath(sessionRoot)
	if err != nil {
		return nil, fmt.Errorf("invalid session root: %w", err)
	}

	args := []string{
		"--die-with-parent",
		"--unshare-pid",
		"--unshare-ipc",
		"--unshare-uts",
		"--proc", "/proc",
		"--dev", "/dev",
		"--tmpfs", "/tmp",
		"--dir", "/etc",
		"--dir", "/etc/ssl",
		"--dir", "/mcon",
	}
	args = appendExistingReadOnlyBinds(args, []string{"/usr", "/bin", "/lib", "/lib64"})
	args = append(args, "--dir", workspace)
	if networkMode == "none" {
		args = append(args, "--unshare-net")
	}

	fileRules := append([]FileRule(nil), spec.Sandbox.Files...)
	sort.SliceStable(fileRules, func(i int, j int) bool {
		return len(fileRules[i].Path) < len(fileRules[j].Path)
	})
	for _, rule := range fileRules {
		ruleArgs, err := buildFileRuleArgs(rule, workspace, cleanSessionRoot, spec.Sandbox.SessionID)
		if err != nil {
			return nil, err
		}
		args = append(args, ruleArgs...)
	}
	for _, rule := range spec.Sandbox.RuntimeFiles {
		ruleArgs, err := buildRuntimeFileRuleArgs(rule, workspace)
		if err != nil {
			return nil, err
		}
		args = append(args, ruleArgs...)
	}
	args = append(args, "--chdir", cwd, "--")
	args = append(args, spec.Argv...)
	return args, nil
}

func buildRuntimeFileRuleArgs(rule FileRule, workspace string) ([]string, error) {
	path, err := cleanAbsolutePath(rule.Path)
	if err != nil {
		return nil, fmt.Errorf("invalid runtime file rule path: %w", err)
	}
	if pathWithin(workspace, path) {
		return nil, fmt.Errorf("runtime file rule must be outside workspace: %s", path)
	}
	if !allowedRuntimePath(path) {
		return nil, fmt.Errorf("runtime file rule path is not allowlisted: %s", path)
	}
	switch rule.Action {
	case "read":
		return []string{"--ro-bind-try", path, path}, nil
	case "edit":
		if !pathWithin("/mcon/provider-runtime", path) {
			return nil, fmt.Errorf("writable runtime file rule must be inside provider runtime: %s", path)
		}
		return []string{"--bind-try", path, path}, nil
	default:
		return nil, fmt.Errorf("unsupported runtime file rule action: %s", rule.Action)
	}
}

func allowedRuntimePath(path string) bool {
	if pathWithin("/mcon/codex-home/packages", path) {
		return true
	}
	if pathWithin("/mcon/provider-runtime", path) {
		return true
	}
	switch path {
	case "/etc/ssl/certs", "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/gai.conf":
		return true
	default:
		return false
	}
}

func appendExistingReadOnlyBinds(args []string, paths []string) []string {
	for _, path := range paths {
		if _, err := os.Stat(path); err == nil {
			args = append(args, "--ro-bind", path, path)
		}
	}
	return args
}

func buildFileRuleArgs(rule FileRule, workspace string, sessionRoot string, sessionID string) ([]string, error) {
	path, err := cleanAbsolutePath(rule.Path)
	if err != nil {
		return nil, fmt.Errorf("invalid file rule path: %w", err)
	}
	if !pathWithin(workspace, path) {
		return nil, fmt.Errorf("file rule path must be inside workspace: %s", path)
	}
	switch rule.Action {
	case "read":
		return []string{"--ro-bind-try", path, path}, nil
	case "edit":
		return []string{"--bind-try", path, path}, nil
	case "write":
		source, err := sessionWriteSource(sessionRoot, sessionID, workspace, path)
		if err != nil {
			return nil, err
		}
		if err := os.MkdirAll(source, 0700); err != nil {
			return nil, fmt.Errorf("create session write directory: %w", err)
		}
		return []string{"--bind", source, path}, nil
	case "deny":
		if _, err := os.Stat(path); errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return []string{"--tmpfs", path}, nil
	default:
		return nil, fmt.Errorf("unsupported file rule action: %s", rule.Action)
	}
}

type sessionManifest struct {
	Created []string `json:"created"`
}

func applySessionWrites(spec ExecSpec) error {
	workspace := spec.Sandbox.Workspace
	if workspace == "" {
		workspace = "/workspace"
	}
	cleanWorkspace, err := cleanAbsolutePath(workspace)
	if err != nil {
		return fmt.Errorf("invalid workspace: %w", err)
	}
	sessionRoot := spec.Sandbox.SessionRoot
	if sessionRoot == "" {
		sessionRoot = "/mcon/session-fs"
	}
	cleanSessionRoot, err := cleanAbsolutePath(sessionRoot)
	if err != nil {
		return fmt.Errorf("invalid session root: %w", err)
	}

	writeRules := make([]FileRule, 0)
	for _, rule := range spec.Sandbox.Files {
		if rule.Action == "write" {
			writeRules = append(writeRules, rule)
		}
	}
	if len(writeRules) == 0 {
		return nil
	}
	if err := validateSessionID(spec.Sandbox.SessionID); err != nil {
		return err
	}
	lock, err := acquireApplyLock(cleanSessionRoot, cleanWorkspace)
	if err != nil {
		return err
	}
	defer lock.Close()

	manifest, err := loadSessionManifest(cleanSessionRoot, spec.Sandbox.SessionID)
	if err != nil {
		return err
	}
	workspaceManifest, err := loadWorkspaceManifest(cleanSessionRoot, cleanWorkspace)
	if err != nil {
		return err
	}
	for _, rule := range writeRules {
		destination, err := cleanAbsolutePath(rule.Path)
		if err != nil {
			return fmt.Errorf("invalid write rule path: %w", err)
		}
		if !pathWithin(cleanWorkspace, destination) {
			return fmt.Errorf("write rule path must be inside workspace: %s", destination)
		}
		source, err := sessionWriteSource(cleanSessionRoot, spec.Sandbox.SessionID, cleanWorkspace, destination)
		if err != nil {
			return err
		}
		if _, err := os.Stat(source); errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err := applySessionTree(source, destination, cleanWorkspace, manifest, workspaceManifest); err != nil {
			return err
		}
	}
	if err := saveSessionManifest(cleanSessionRoot, spec.Sandbox.SessionID, manifest); err != nil {
		return err
	}
	return saveWorkspaceManifest(cleanSessionRoot, cleanWorkspace, workspaceManifest)
}

type applyLock struct {
	file *os.File
}

func acquireApplyLock(sessionRoot string, workspace string) (*applyLock, error) {
	lockRoot := filepath.Join(sessionRoot, "_locks")
	if err := os.MkdirAll(lockRoot, 0700); err != nil {
		return nil, fmt.Errorf("create apply lock root: %w", err)
	}
	sum := sha256.Sum256([]byte(filepath.Clean(workspace)))
	lockPath := filepath.Join(lockRoot, hex.EncodeToString(sum[:])+".lock")
	file, err := os.OpenFile(lockPath, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, fmt.Errorf("open apply lock: %w", err)
	}
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX); err != nil {
		file.Close()
		return nil, fmt.Errorf("lock workspace apply: %w", err)
	}
	return &applyLock{file: file}, nil
}

func (lock *applyLock) Close() error {
	unlockErr := syscall.Flock(int(lock.file.Fd()), syscall.LOCK_UN)
	closeErr := lock.file.Close()
	if unlockErr != nil {
		return unlockErr
	}
	return closeErr
}

func applySessionTree(sourceRoot string, destinationRoot string, workspaceRoot string, sessionManifest map[string]bool, workspaceManifest map[string]bool) error {
	return filepath.WalkDir(sourceRoot, func(sourcePath string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		relative, err := filepath.Rel(sourceRoot, sourcePath)
		if err != nil {
			return err
		}
		if relative == "." {
			return nil
		}
		if !validRelativePath(relative) {
			return fmt.Errorf("session path escaped source root: %s", relative)
		}
		if entry.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("session output symlinks are not allowed: %s", relative)
		}
		destinationPath := filepath.Join(destinationRoot, relative)
		if !pathWithin(destinationRoot, destinationPath) {
			return fmt.Errorf("session output escaped destination root: %s", destinationPath)
		}
		workspaceRelative, err := filepath.Rel(workspaceRoot, destinationPath)
		if err != nil {
			return err
		}
		if !validRelativePath(workspaceRelative) {
			return fmt.Errorf("session output escaped workspace root: %s", destinationPath)
		}
		key := filepath.ToSlash(workspaceRelative)
		if entry.IsDir() {
			return applySessionDirectory(destinationRoot, destinationPath, key, sessionManifest, workspaceManifest)
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return fmt.Errorf("unsupported session output file type: %s", relative)
		}
		return applySessionFile(destinationRoot, sourcePath, destinationPath, key, info.Mode().Perm(), sessionManifest, workspaceManifest)
	})
}

func applySessionDirectory(destinationRoot string, destinationPath string, key string, sessionManifest map[string]bool, workspaceManifest map[string]bool) error {
	if err := rejectSymlinkParent(destinationRoot, destinationPath); err != nil {
		return err
	}
	info, err := os.Lstat(destinationPath)
	if err == nil {
		if info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("refusing to write through host symlink: %s", destinationPath)
		}
		if !info.IsDir() {
			return fmt.Errorf("refusing to replace existing host file with directory: %s", destinationPath)
		}
		return nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := os.MkdirAll(destinationPath, 0700); err != nil {
		return err
	}
	sessionManifest[key] = true
	workspaceManifest[key] = true
	return nil
}

func applySessionFile(destinationRoot string, sourcePath string, destinationPath string, key string, mode fs.FileMode, sessionManifest map[string]bool, workspaceManifest map[string]bool) error {
	if err := rejectSymlinkParent(destinationRoot, destinationPath); err != nil {
		return err
	}
	flags := os.O_WRONLY | os.O_CREATE
	if info, err := os.Lstat(destinationPath); err == nil {
		if info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("refusing to write through host symlink: %s", destinationPath)
		}
		if info.IsDir() {
			return fmt.Errorf("refusing to replace existing host directory with file: %s", destinationPath)
		}
		if !sessionManifest[key] && !workspaceManifest[key] {
			return fmt.Errorf("refusing to overwrite host path not created by mcon: %s", destinationPath)
		}
		flags |= os.O_TRUNC
	} else if errors.Is(err, os.ErrNotExist) {
		if err := os.MkdirAll(filepath.Dir(destinationPath), 0700); err != nil {
			return err
		}
		flags |= os.O_EXCL
		sessionManifest[key] = true
		workspaceManifest[key] = true
	} else {
		return err
	}

	source, err := os.Open(sourcePath)
	if err != nil {
		return err
	}
	defer source.Close()
	destination, err := os.OpenFile(destinationPath, flags, mode)
	if err != nil {
		if errors.Is(err, os.ErrExist) {
			return fmt.Errorf("refusing to overwrite concurrently created host path: %s", destinationPath)
		}
		return err
	}
	_, copyErr := io.Copy(destination, source)
	closeErr := destination.Close()
	if copyErr != nil {
		return copyErr
	}
	return closeErr
}

func rejectSymlinkParent(root string, path string) error {
	root = filepath.Clean(root)
	parent := filepath.Dir(filepath.Clean(path))
	if parent == root {
		return nil
	}
	relative, err := filepath.Rel(root, parent)
	if err != nil {
		return err
	}
	if !validRelativePath(relative) {
		return fmt.Errorf("destination parent escaped root: %s", parent)
	}
	current := root
	for _, part := range strings.Split(relative, string(os.PathSeparator)) {
		current = filepath.Join(current, part)
		info, err := os.Lstat(current)
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("refusing to write below host symlink: %s", current)
		}
	}
	return nil
}

func loadSessionManifest(sessionRoot string, sessionID string) (map[string]bool, error) {
	return loadManifest(sessionManifestPath(sessionRoot, sessionID), "session")
}

func loadWorkspaceManifest(sessionRoot string, workspace string) (map[string]bool, error) {
	return loadManifest(workspaceManifestPath(sessionRoot, workspace), "workspace")
}

func loadManifest(path string, kind string) (map[string]bool, error) {
	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return map[string]bool{}, nil
	}
	if err != nil {
		return nil, err
	}
	var manifest sessionManifest
	if err := json.Unmarshal(raw, &manifest); err != nil {
		return nil, fmt.Errorf("decode %s manifest: %w", kind, err)
	}
	result := make(map[string]bool, len(manifest.Created))
	for _, item := range manifest.Created {
		if !validRelativePath(filepath.FromSlash(item)) {
			return nil, fmt.Errorf("invalid %s manifest path: %s", kind, item)
		}
		result[item] = true
	}
	return result, nil
}

func saveSessionManifest(sessionRoot string, sessionID string, created map[string]bool) error {
	return saveManifest(sessionManifestPath(sessionRoot, sessionID), created)
}

func saveWorkspaceManifest(sessionRoot string, workspace string, created map[string]bool) error {
	return saveManifest(workspaceManifestPath(sessionRoot, workspace), created)
}

func saveManifest(path string, created map[string]bool) error {
	items := make([]string, 0, len(created))
	for item := range created {
		items = append(items, item)
	}
	sort.Strings(items)
	raw, err := json.MarshalIndent(sessionManifest{Created: items}, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	temporaryPath := fmt.Sprintf("%s.tmp.%d", path, os.Getpid())
	if err := os.WriteFile(temporaryPath, append(raw, '\n'), 0600); err != nil {
		return err
	}
	return os.Rename(temporaryPath, path)
}

func sessionManifestPath(sessionRoot string, sessionID string) string {
	return filepath.Join(sessionRoot, sessionID, "manifest.json")
}

func workspaceManifestPath(sessionRoot string, workspace string) string {
	sum := sha256.Sum256([]byte(filepath.Clean(workspace)))
	return filepath.Join(sessionRoot, "_workspaces", hex.EncodeToString(sum[:]), "manifest.json")
}

func cleanAbsolutePath(path string) (string, error) {
	if path == "" {
		return "", errors.New("path must not be empty")
	}
	if !filepath.IsAbs(path) {
		return "", fmt.Errorf("path must be absolute: %s", path)
	}
	return filepath.Clean(path), nil
}

func sessionWriteSource(sessionRoot string, sessionID string, workspace string, path string) (string, error) {
	if err := validateSessionID(sessionID); err != nil {
		return "", err
	}
	relative, err := filepath.Rel(workspace, path)
	if err != nil {
		return "", fmt.Errorf("resolve write path relative to workspace: %w", err)
	}
	if relative == "." {
		relative = "_workspace"
	}
	if strings.HasPrefix(relative, ".."+string(os.PathSeparator)) || relative == ".." || filepath.IsAbs(relative) {
		return "", fmt.Errorf("write path must be inside workspace: %s", path)
	}
	sessionRoot = filepath.Clean(sessionRoot)
	source := filepath.Join(sessionRoot, sessionID, "fs", relative)
	if !pathWithin(filepath.Join(sessionRoot, sessionID), source) {
		return "", fmt.Errorf("resolved session path escaped session root: %s", source)
	}
	return source, nil
}

func validRelativePath(path string) bool {
	if path == "" || filepath.IsAbs(path) {
		return false
	}
	clean := filepath.Clean(path)
	return clean != "." && clean != ".." && !strings.HasPrefix(clean, ".."+string(os.PathSeparator))
}

func validateSessionID(sessionID string) error {
	if sessionID == "" {
		return errors.New("sandbox.session_id is required when write permissions are used")
	}
	for _, char := range sessionID {
		if (char >= 'a' && char <= 'z') || (char >= 'A' && char <= 'Z') || (char >= '0' && char <= '9') || char == '_' || char == '-' {
			continue
		}
		return fmt.Errorf("sandbox.session_id contains unsupported character: %q", char)
	}
	return nil
}

func pathWithin(root string, path string) bool {
	root = filepath.Clean(root)
	path = filepath.Clean(path)
	return path == root || strings.HasPrefix(path, root+string(os.PathSeparator))
}
