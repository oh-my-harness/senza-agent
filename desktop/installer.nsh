; ──────────────────────────────────────────────────────────────────────────
; senza-agent NSIS installer include script
;
; Hooked into electron-builder's NSIS template via "nsis.include" in
; package.json.  Runs after files are extracted, before the installer
; finishes.
;
; What it does:
;   1. Runs setup_python.ps1 to create a venv and install dependencies.
;   2. The venv lives in $INSTDIR\python_venv so it survives upgrades.
;
; $INSTDIR is the user-chosen installation directory (step 1 of the wizard).
; ──────────────────────────────────────────────────────────────────────────

!macro customInstall
  ; Run the Python venv setup script.
  ; -ExecutionPolicy Bypass: allow the script to run without prompting.
  DetailPrint "Setting up Python virtual environment..."
  nsExec::ExecToLog 'powershell.exe -ExecutionPolicy Bypass -NoProfile -File "$INSTDIR\resources\setup_python.ps1" -InstallDir "$INSTDIR"'
  Pop $0
  ${If} $0 != 0
    DetailPrint "WARNING: Python setup exited with code $0. The app will retry on first launch."
  ${Else}
    DetailPrint "Python environment ready."
  ${EndIf}
!macroend

; Clean up the venv on uninstall (optional — user data in $INSTDIR is removed
; anyway, but this is explicit).
!macro customUnInstall
  ${If} ${FileExists} "$INSTDIR\python_venv"
    DetailPrint "Removing Python virtual environment..."
    RMDir /r "$INSTDIR\python_venv"
  ${EndIf}
!macroend
