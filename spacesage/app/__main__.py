"""``python -m spacesage.app`` -- run the desktop application from a checkout."""

from __future__ import annotations

if __name__ == "__main__":
    # The crash net imports before the application: it is stdlib-only, so it
    # still loads when the Qt-dependent app cannot, and every failure between
    # here and the event loop then leaves a readable log instead of the
    # bootloader's one-line box (see ``spacesage.app.crash``).
    from spacesage.app.crash import report_crash

    try:
        from spacesage.app import main

        code = main()
    except Exception as exc:
        code = report_crash(exc)
    raise SystemExit(code)
