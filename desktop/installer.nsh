; ──────────────────────────────────────────────────────────────────────────
; senza-agent NSIS installer include script
;
; Hooked into electron-builder's NSIS template via "nsis.include" in
; package.json.
;
;   Design decision:
;     Python venv creation + pip install is NOT done here.  It takes 30-120s
;     (downloading wheels), and nsExec blocks the NSIS progress bar during
;     that time, causing it to jump/rewind erratically with no feedback.
;     Instead, the app creates the venv on first launch via main.js
;     (ensurePythonVenv), which shows a proper loading window with status
;     text.  The installer shows the details pane (customHeader) and a closing
;     narration line (customInstall); per-file extraction lines stay hidden by
;     the template's "SetDetailsPrint none".
;
;   Shortcuts (desktop + start menu) are created by electron-builder's
;   NSIS template based on package.json config.  The NSIS template
;   doesn't support a shortcut-choice page, so both are created; users
;   can delete unwanted ones manually.
!macro customHeader
  ; electron-builder's common.nsh sets "ShowInstDetails nevershow", which hides
  ; the details listbox entirely. NSIS attributes are last-one-wins, and the
  ; user include is expanded after common.nsh, so this overrides it and makes
  ; the details pane (with our DetailPrint narration) visible in the wizard.
  ShowInstDetails show
  ShowUnInstDetails show
!macroend

; Runs at the very end of the main install section (installSection.nsh), AFTER
; the template's "SetDetailsPrint none" (installSection.nsh line 6), which
; silences ALL output incl. DetailPrint (NSIS routes DetailPrint through the
; same status-update gate). Re-enable printing first, then narrate. Nothing
; executes after this hook in the section, so no print-mode cleanup needed.
!macro customInstall
  SetDetailsPrint both
  DetailPrint "Program files extracted. Finalizing installation..."
!macroend

; Clean up the venv on uninstall.
!macro customUnInstall
  ${If} ${FileExists} "$INSTDIR\python_venv"
    DetailPrint "Removing Python virtual environment..."
    RMDir /r "$INSTDIR\python_venv"
  ${EndIf}
!macroend
