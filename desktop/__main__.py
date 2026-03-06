"""
@file __main__.py
@brief Entry point for the CodeBrain Desktop application.

Run with:
    python -m desktop

from the project root (GrayMatter/). The project root must be the working
directory so that codebrain.toml and the local .env/codebrain.toml override
are discoverable by load_config().
"""

import sys

from PySide6.QtWidgets import QApplication

from desktop.app import CodeBrainApp


def main() -> None:
    """@brief Construct the QApplication, run the CodeBrainApp, and enter the event loop.

    Sets QuitOnLastWindowClosed to False so the app persists in the system
    tray when all windows are closed and at least one watcher is active.
    """
    app = QApplication(sys.argv)
    app.setApplicationName("CodeBrain Desktop")
    app.setApplicationDisplayName("CodeBrain Desktop")
    app.setOrganizationName("CodeBrain")
    app.setQuitOnLastWindowClosed(False)

    codebrain_app = CodeBrainApp()
    codebrain_app.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
