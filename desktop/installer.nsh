; ──────────────────────────────────────────────────────────────────────────
; senza-agent NSIS installer include script
;
; Hooked into electron-builder's NSIS template via "nsis.include" in
; package.json.
;
; Design decision:
;   Python venv creation + pip install is NOT done here.  It takes 30-120s
;   (downloading wheels), and nsExec blocks the NSIS progress bar during
;   that time, causing it to jump/rewind erratically with no feedback.
;
;   Instead, the app creates the venv on first launch via main.js
;   (ensurePythonVenv), which shows a proper loading window with status
;   text.  The installer just extracts files and finishes quickly.
;
;   Shortcuts (desktop + start menu) are created by electron-builder's
;   NSIS template based on package.json config.  The NSIS template
;   doesn't support a shortcut-choice page, so both are created; users
;   can delete unwanted ones manually.
; ──────────────────────────────────────────────────────────────────────────

!macro customInstall
  DetailPrint "Files installed. Python environment will be set up on first launch."
!macroend

; Clean up the venv on uninstall.
!macro customUnInstall
  ${If} ${FileExists} "$INSTDIR\python_venv"
    DetailPrint "Removing Python virtual environment..."
    RMDir /r "$INSTDIR\python_venv"
  ${EndIf}
!macroend
