:cmd /c "%~dp0git.bat" -v3 -u push git@github.com:QGB/git.bat.git %*
cd /d "%~dp0"
cmd /c "%~dp0git.bat" -v 3 -r 99 -u push git@github.com:QGB/git.bat.git %*

:echo %0 %* end. exit

pause
