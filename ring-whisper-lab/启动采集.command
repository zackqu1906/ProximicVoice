#!/bin/zsh
set -eu
project_dir="${0:A:h}"
if [[ -x "$project_dir/.venv/bin/python" ]]; then
  exec "$project_dir/.venv/bin/python" "$project_dir/run.py" gui
elif [[ -x "$project_dir/../.runtime/venv/bin/python" ]]; then
  exec "$project_dir/../.runtime/venv/bin/python" "$project_dir/run.py" gui
else
  print '请先按 README 创建 Python 环境并安装项目依赖。'
  read -r '?按回车退出'
fi
