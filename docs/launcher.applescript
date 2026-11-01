-- Source of Workstation.app (build it in the repository folder: osacompile -o Workstation.app docs/launcher.applescript).
-- Starts the workstation window in the background and returns at once; the log goes to outputs/app.log.
-- The app runs the repository it sits in, so keep Workstation.app in that folder (drag it to the Dock from there).
-- Only the python command is backgrounded and none of its streams stay on this applet's pipe, so the applet exits
-- at once (with "a && b &" a background shell kept the pipe open and the applet blocked sleep for an hour).
set repo to do shell script "dirname " & quoted form of POSIX path of (path to me)
do shell script "cd " & quoted form of repo & "; mkdir -p outputs; nohup .venv/bin/python start.py >> outputs/app.log 2>&1 < /dev/null &"
