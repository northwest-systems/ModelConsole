# Source this file from your shell to use the local mcon host command.

_mcon_repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case ":$PATH:" in
  *":$_mcon_repo_root/bin:"*) ;;
  *) export PATH="$_mcon_repo_root/bin:$PATH" ;;
esac

# MCON_DATA_DIR: ホスト実行時の config/runtime/vault/audit を repo 配下へ置く。
export MCON_DATA_DIR="${MCON_DATA_DIR:-$_mcon_repo_root/data}"

# UV_CACHE_DIR: uv のキャッシュ先。Docker build ではなく host wrapper 用。
export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/uv-cache}"

unset _mcon_repo_root
