#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${SDN_CONTROLLER_PYTHON:-${project_root}/.venv/bin/python}"
if [[ ! -x "${python_bin}" ]]; then
    printf 'Controller Python is not executable: %s\n' "${python_bin}" >&2
    exit 2
fi

"${python_bin}" -c 'import os,sys; os.environ.setdefault("EVENTLET_NO_GREENDNS", "yes"); import eventlet.wsgi; eventlet.wsgi.ALREADY_HANDLED = getattr(eventlet.wsgi, "ALREADY_HANDLED", object()); import ryu.cmd.manager, ryu.ofproto.ofproto_v1_3; assert sys.version_info[:2] == (3, 10), "Ryu requires Python 3.10"' || {
    printf 'Controller Python must be Python 3.10 with the required Ryu modules.\n' >&2
    exit 2
}

exec "${python_bin}" "${project_root}/tools/ryu_manager.py" "$@"
