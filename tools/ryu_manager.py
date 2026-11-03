#!/usr/bin/env python3
"""Compatibility entry point for Ryu 4.34 with Eventlet 0.33.x.

Ryu imports eventlet.wsgi.ALREADY_HANDLED, removed from newer Eventlet.
Ryu's SDN application does not use Eventlet's DNS patch, so disable its
legacy dnspython integration before Eventlet imports it.
"""
import os

os.environ.setdefault("EVENTLET_NO_GREENDNS", "yes")

import eventlet.wsgi

if not hasattr(eventlet.wsgi, "ALREADY_HANDLED"):
    eventlet.wsgi.ALREADY_HANDLED = object()

import sys
from pathlib import Path

from ryu.cmd.manager import main

if __name__ == "__main__":
    flags_file = str(Path(__file__).with_name("ryu_user_flags.py"))
    if "--user-flags" not in sys.argv:
        sys.argv[1:1] = ["--user-flags", flags_file]
    main()
