package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRunExecutesCommandWithProvidedEnv(t *testing.T) {
	input := strings.NewReader(`{"argv":["/bin/sh","-c","printf %s \"$MCON_TEST\""],"env":{"MCON_TEST":"ok"}}`)
	var stdout bytes.Buffer
	var stderr bytes.Buffer

	if err := run(input, &stdout, &stderr); err != nil {
		t.Fatalf("run returned error: %v, stderr=%s", err, stderr.String())
	}
	if stdout.String() != "ok" {
		t.Fatalf("stdout = %q, want ok", stdout.String())
	}
}

func TestRunRejectsEmptyArgv(t *testing.T) {
	input := strings.NewReader(`{"argv":[]}`)
	var stdout bytes.Buffer
	var stderr bytes.Buffer

	if err := run(input, &stdout, &stderr); err == nil {
		t.Fatal("run returned nil error for empty argv")
	}
}

func TestBuildCommandWrapsSandboxedCommandWithBubblewrap(t *testing.T) {
	workspace := t.TempDir()
	secretsPath := filepath.Join(workspace, "secrets")
	if err := os.MkdirAll(secretsPath, 0700); err != nil {
		t.Fatalf("create secrets path: %v", err)
	}
	sessionRoot := t.TempDir()
	docsPath := filepath.Join(workspace, "docs")
	generatedPath := filepath.Join(workspace, "generated")
	spec := ExecSpec{
		Argv: []string{"/usr/bin/env"},
		Cwd:  docsPath,
		Env:  map[string]string{"PATH": "/usr/bin:/bin"},
		Sandbox: SandboxSpec{
			Enabled:     true,
			Workspace:   workspace,
			Network:     "none",
			SessionID:   "test-session",
			SessionRoot: sessionRoot,
			Files: []FileRule{
				{Action: "read", Path: workspace},
				{Action: "deny", Path: secretsPath},
				{Action: "edit", Path: docsPath},
				{Action: "write", Path: generatedPath},
			},
		},
	}

	command, err := buildCommand(spec)
	if err != nil {
		t.Fatalf("buildCommand returned error: %v", err)
	}

	if command.Path != "bwrap" {
		t.Fatalf("command path = %q, want bwrap", command.Path)
	}
	joined := strings.Join(command.Args, " ")
	for _, expected := range []string{
		"--unshare-net",
		"--ro-bind-try " + workspace + " " + workspace,
		"--tmpfs " + secretsPath,
		"--bind-try " + docsPath + " " + docsPath,
		"--bind " + filepath.Join(sessionRoot, "test-session", "fs", "generated") + " " + generatedPath,
		"--chdir " + docsPath + " -- /usr/bin/env",
	} {
		if !strings.Contains(joined, expected) {
			t.Fatalf("bubblewrap args missing %q in %q", expected, joined)
		}
	}
}

func TestBuildCommandRejectsCwdOutsideWorkspace(t *testing.T) {
	_, err := buildCommand(
		ExecSpec{
			Argv: []string{"/bin/true"},
			Cwd:  "/app",
			Sandbox: SandboxSpec{
				Enabled:   true,
				Workspace: "/workspace",
			},
		},
	)
	if err == nil {
		t.Fatal("buildCommand returned nil error for cwd outside workspace")
	}
}

func TestBuildCommandRequiresSessionIDForWriteRules(t *testing.T) {
	_, err := buildCommand(
		ExecSpec{
			Argv: []string{"/bin/true"},
			Cwd:  "/workspace",
			Sandbox: SandboxSpec{
				Enabled:     true,
				Workspace:   "/workspace",
				SessionRoot: t.TempDir(),
				Files: []FileRule{
					{Action: "write", Path: "/workspace/generated"},
				},
			},
		},
	)
	if err == nil {
		t.Fatal("buildCommand returned nil error for write rule without session id")
	}
}

func TestApplySessionWritesCopiesNewFilesAndAllowsSessionEdits(t *testing.T) {
	workspace := t.TempDir()
	generatedPath := filepath.Join(workspace, "generated")
	if err := os.MkdirAll(generatedPath, 0700); err != nil {
		t.Fatalf("create generated path: %v", err)
	}
	sessionRoot := t.TempDir()
	spec := ExecSpec{
		Sandbox: SandboxSpec{
			Enabled:     true,
			Workspace:   workspace,
			SessionID:   "agent-session",
			SessionRoot: sessionRoot,
			Files: []FileRule{
				{Action: "write", Path: generatedPath},
			},
		},
	}
	source, err := sessionWriteSource(sessionRoot, "agent-session", workspace, generatedPath)
	if err != nil {
		t.Fatalf("sessionWriteSource returned error: %v", err)
	}
	if err := os.MkdirAll(source, 0700); err != nil {
		t.Fatalf("create session source: %v", err)
	}
	if err := os.WriteFile(filepath.Join(source, "created.txt"), []byte("first"), 0600); err != nil {
		t.Fatalf("write session file: %v", err)
	}

	if err := applySessionWrites(spec); err != nil {
		t.Fatalf("applySessionWrites returned error: %v", err)
	}
	hostPath := filepath.Join(generatedPath, "created.txt")
	if got, err := os.ReadFile(hostPath); err != nil || string(got) != "first" {
		t.Fatalf("host file = %q, err=%v; want first", string(got), err)
	}

	if err := os.WriteFile(filepath.Join(source, "created.txt"), []byte("edited"), 0600); err != nil {
		t.Fatalf("edit session file: %v", err)
	}
	if err := applySessionWrites(spec); err != nil {
		t.Fatalf("second applySessionWrites returned error: %v", err)
	}
	if got, err := os.ReadFile(hostPath); err != nil || string(got) != "edited" {
		t.Fatalf("edited host file = %q, err=%v; want edited", string(got), err)
	}
}

func TestApplySessionWritesRejectsExistingHostFileNotCreatedBySession(t *testing.T) {
	workspace := t.TempDir()
	generatedPath := filepath.Join(workspace, "generated")
	if err := os.MkdirAll(generatedPath, 0700); err != nil {
		t.Fatalf("create generated path: %v", err)
	}
	hostPath := filepath.Join(generatedPath, "preexisting.txt")
	if err := os.WriteFile(hostPath, []byte("host"), 0600); err != nil {
		t.Fatalf("write host file: %v", err)
	}
	sessionRoot := t.TempDir()
	spec := ExecSpec{
		Sandbox: SandboxSpec{
			Enabled:     true,
			Workspace:   workspace,
			SessionID:   "agent-session",
			SessionRoot: sessionRoot,
			Files: []FileRule{
				{Action: "write", Path: generatedPath},
			},
		},
	}
	source, err := sessionWriteSource(sessionRoot, "agent-session", workspace, generatedPath)
	if err != nil {
		t.Fatalf("sessionWriteSource returned error: %v", err)
	}
	if err := os.MkdirAll(source, 0700); err != nil {
		t.Fatalf("create session source: %v", err)
	}
	if err := os.WriteFile(filepath.Join(source, "preexisting.txt"), []byte("session"), 0600); err != nil {
		t.Fatalf("write session file: %v", err)
	}

	if err := applySessionWrites(spec); err == nil {
		t.Fatal("applySessionWrites returned nil error for preexisting host file")
	}
	if got, err := os.ReadFile(hostPath); err != nil || string(got) != "host" {
		t.Fatalf("host file = %q, err=%v; want host", string(got), err)
	}
}

func TestApplySessionWritesAllowsLastWinForManagedHostPath(t *testing.T) {
	workspace := t.TempDir()
	generatedPath := filepath.Join(workspace, "generated")
	if err := os.MkdirAll(generatedPath, 0700); err != nil {
		t.Fatalf("create generated path: %v", err)
	}
	sessionRoot := t.TempDir()
	buildSpec := func(sessionID string, content string) ExecSpec {
		spec := ExecSpec{
			Sandbox: SandboxSpec{
				Enabled:     true,
				Workspace:   workspace,
				SessionID:   sessionID,
				SessionRoot: sessionRoot,
				Files: []FileRule{
					{Action: "write", Path: generatedPath},
				},
			},
		}
		source, err := sessionWriteSource(sessionRoot, sessionID, workspace, generatedPath)
		if err != nil {
			t.Fatalf("sessionWriteSource returned error: %v", err)
		}
		if err := os.MkdirAll(source, 0700); err != nil {
			t.Fatalf("create session source: %v", err)
		}
		if err := os.WriteFile(filepath.Join(source, "race.txt"), []byte(content), 0600); err != nil {
			t.Fatalf("write session file: %v", err)
		}
		return spec
	}
	specA := buildSpec("agent-a", "a")
	specB := buildSpec("agent-b", "b")
	start := make(chan struct{})
	errorsCh := make(chan error, 2)
	for _, spec := range []ExecSpec{specA, specB} {
		go func(spec ExecSpec) {
			<-start
			errorsCh <- applySessionWrites(spec)
		}(spec)
	}
	close(start)
	errA := <-errorsCh
	errB := <-errorsCh

	successes := 0
	for _, err := range []error{errA, errB} {
		if err == nil {
			successes++
		}
	}
	if successes != 2 {
		t.Fatalf("successes = %d, errors = %v / %v; want both sessions to succeed", successes, errA, errB)
	}
	got, err := os.ReadFile(filepath.Join(generatedPath, "race.txt"))
	if err != nil {
		t.Fatalf("read host race file: %v", err)
	}
	if string(got) != "a" && string(got) != "b" {
		t.Fatalf("host race file = %q, want a or b", string(got))
	}
}
