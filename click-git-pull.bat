cd /d "%~dp0"
cmd /c "%~dp0git.bat" -v 3 -r 99 -u pull git@github.com:QGB/git.bat.git %*

pause