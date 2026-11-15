#!/bin/bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
"$script_dir/../backend/.venv/bin/python" "$script_dir/synthetic_deepseek.py" configure-key
printf '\n配置完成，可关闭此窗口。\n'
read -r -p '按回车结束。'
