:cmd /c "%~dp0git.bat" -v3 -u push git@github.com:QGB/git.bat.git %*
cmd /c "%~dp0git.bat" -v3 -u push %*

:echo %0 %* end. exit

pause
