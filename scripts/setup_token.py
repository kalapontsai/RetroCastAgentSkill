#!/usr/bin/env python3
"""scripts/setup_token.py — 首次安裝 helper：把 FinMind token 寫到 ~/.config/retrocast/finmind-token。

設計理由:
  - OpenClaw secrets store get 在 env-kind 模式只回 redacted preview
    ('eyJ0eX…RlnQ'),不是完整 JWT
  - 所以 skill runtime 實際依賴 ~/.config/retrocast/finmind-token
  - 這個檔必須在首次安裝時建立好 (而且 chmod 600)

Workflow:
  $ python scripts/setup_token.py
  1. 試從 OpenClaw secrets 讀 FINMIND_TOKEN
     - 若讀到且長度足夠 (>= 50,代表完整 token),寫到本地檔
     - 若只回 preview (含 '…'),提示 user 手動貼
  2. 若 OpenClaw 完全讀不到,提示 user 手動貼 token
  3. 寫到 ~/.config/retrocast/finmind-token (chmod 600)
  4. 跑 retrocast_cli.py health 驗證 finmind_token: true
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

DEST = Path.home() / '.config' / 'retrocast' / 'finmind-token'
MIN_TOKEN_LEN = 50  # 完整 JWT ~169 chars;preview ~11 chars


def _try_openclaw() -> str | None:
    """Try to get token from OpenClaw secrets. Returns full token or None."""
    try:
        result = subprocess.run(
            ['openclaw', 'secrets', 'store', 'get', 'FINMIND_TOKEN'],
            capture_output=True, text=True, check=True, timeout=5,
        )
        output = result.stdout.strip()
        if '=' in output:
            output = output.split('=', 1)[1].strip()
        # Preview 會含 Unicode 省略號 '…' 或長度太短
        if '…' in output or len(output) < MIN_TOKEN_LEN:
            return None
        return output
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _prompt_user() -> str:
    """Prompt user to paste token (not echoed to history)."""
    print('請貼 FinMind v4 token (輸入隱藏,token 不會留在 shell history):')
    try:
        import getpass
        token = getpass.getpass('Token: ').strip()
    except (ImportError, KeyboardInterrupt):
        # fallback
        print('(getpass 不可用,改用一般輸入 — token 會留在 history!)')
        token = input('Token: ').strip()
    return token


def _write_token(token: str) -> None:
    """Write token to DEST with umask 077."""
    DEST.parent.mkdir(parents=True, exist_ok=True)
    # 用 umask 077 確保檔案預設權限是 600
    old_umask = os.umask(0o077)
    try:
        DEST.write_text(f'FINMIND_TOKEN={token}\n', encoding='utf-8')
    finally:
        os.umask(old_umask)
    # 雙重確認權限
    DEST.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    print(f'\n✓ Token written to {DEST} (chmod 600)')


def _verify() -> bool:
    """Run health check to verify token works."""
    print()
    print('Verifying with retrocast_cli.py health ...')
    skill_root = Path(__file__).resolve().parent.parent
    cli = skill_root / 'scripts' / 'retrocast_cli.py'
    if not cli.exists():
        print(f'  (skip: {cli} not found)')
        return True
    try:
        result = subprocess.run(
            ['.venv/bin/python' if (skill_root / '.venv').exists() else 'python3',
             str(cli), 'health'],
            cwd=str(skill_root), capture_output=True, text=True, timeout=30,
        )
        print(result.stdout)
        return '"finmind_token": true' in result.stdout or '"finmind_token":true' in result.stdout
    except Exception as e:
        print(f'  verify error: {e}')
        return False


def main() -> None:
    print('=== RetroCast FinMind token setup ===\n')

    # Step 1: try OpenClaw secrets
    print('[1/3] 試從 OpenClaw secrets 讀 FINMIND_TOKEN ...')
    token = _try_openclaw()
    if token:
        print(f'  ✓ 讀到完整 token ({len(token)} chars)')
    else:
        print('  ✗ OpenClaw secrets 沒回完整 token (preview-only 或沒設定)')
        print()
        # Step 2: prompt user
        print('[2/3] 手動輸入 token')
        # 這裡也提供「先 set OpenClaw secrets 再回來」的選項
        print('  提示: 你也可以先用 `openclaw secrets store set FINMIND_TOKEN --kind env` 設定後重跑此 script')
        print()
        token = _prompt_user()
        if not token or len(token) < MIN_TOKEN_LEN:
            print(f'ERROR: token 太短 ({len(token) if token else 0} chars),至少 {MIN_TOKEN_LEN}')
            sys.exit(1)

    # Step 3: write
    print()
    print(f'[3/3] 寫到 {DEST}')
    _write_token(token)

    # Verify
    ok = _verify()
    if ok:
        print('\n✓ Setup 完成。retrocast_cli.py 與 (未來) Flask UI 都可正常使用 token。')
    else:
        print('\n⚠ Token 已寫入,但 health 檢查未通過 finmind_token: true')
        print('  可能原因: token 過期、配額限制、或 FinMind API 變動')
        print('  建議: 到 FinMind dashboard 確認 token 狀態')


if __name__ == '__main__':
    main()
