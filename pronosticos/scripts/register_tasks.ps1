# Registra las tareas programadas de Windows del AI Football Predictor (usuario actual, sin ventanas).
#   - Al iniciar sesión: servidor del tablero en http://localhost:8765
#   - 20:00 diario: análisis preliminar de mañana (revisión de Claude ~20:15; listo antes de las 21:00)
#   - 21:00 diario: abre el tablero con los pronósticos
#   - Cada hora de 08:00 a 22:00: alineaciones y cambios de última hora
$ErrorActionPreference = "Stop"
$scripts = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent (Split-Path -Parent $scripts)
$pyw = (Get-Command pythonw).Source
$runner = Join-Path $scripts "run_task.py"
$web = Join-Path (Split-Path -Parent $scripts) "web"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
$folder = "\Pronosticos\"

# 1. Servidor del tablero (siempre activo mientras la sesión esté abierta)
$a = New-ScheduledTaskAction -Execute $pyw -Argument "`"$(Join-Path $scripts 'serve.py')`" 8765" -WorkingDirectory $root
$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 0) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskPath $folder -TaskName "Servidor del tablero" -Action $a -Trigger $t -Settings $s -Force | Out-Null

# 2. Análisis nocturno 20:00
$a = New-ScheduledTaskAction -Execute $pyw -Argument "`"$runner`" nightly" -WorkingDirectory $root
Register-ScheduledTask -TaskPath $folder -TaskName "Analisis nocturno" -Action $a -Trigger (New-ScheduledTaskTrigger -Daily -At 20:00) -Settings $settings -Force | Out-Null

# 3. Presentación 21:00
$a = New-ScheduledTaskAction -Execute $pyw -Argument "`"$runner`" open" -WorkingDirectory $root
Register-ScheduledTask -TaskPath $folder -TaskName "Presentar pronosticos 9pm" -Action $a -Trigger (New-ScheduledTaskTrigger -Daily -At 21:00) -Settings $settings -Force | Out-Null

# 4. Alineaciones y última hora: cada hora de 08:00 a 22:00
$a = New-ScheduledTaskAction -Execute $pyw -Argument "`"$runner`" update" -WorkingDirectory $root
$t = New-ScheduledTaskTrigger -Daily -At 08:00
$t.Repetition = (New-ScheduledTaskTrigger -Once -At 08:00 -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Hours 14)).Repetition
Register-ScheduledTask -TaskPath $folder -TaskName "Alineaciones y ultima hora" -Action $a -Trigger $t -Settings $settings -Force | Out-Null

Get-ScheduledTask -TaskPath $folder | Select-Object TaskName, State

