-- Source of Workstation Chat.app (build it in the repository folder with
-- scripts/build_apps.sh).
-- The same as docs/launcher.applescript, with --chat: the window shows only Chat, Models and Settings. When
-- Workstation.app is open already, it opens only a chat window on that app's server, so the model loads once.
-- The app runs the repository it sits in, so keep Workstation Chat.app in that folder (drag it to the Dock from there).
-- Only the python command is backgrounded and none of its streams stay on this applet's pipe, so the applet exits
-- at once.
set repo to do shell script "dirname " & quoted form of POSIX path of (path to me)
do shell script "cd " & quoted form of repo & "; mkdir -p outputs; nohup .venv/bin/python start.py --chat >> outputs/app.log 2>&1 < /dev/null &"
