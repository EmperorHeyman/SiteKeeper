; Sitekeeper - NSIS installer
; Modern UI 2, per-machine install to Program Files (64-bit).

Unicode true
SetCompressor /SOLID lzma

!define APP_NAME        "Sitekeeper"
!define APP_EXE         "Sitekeeper.exe"
!define APP_VERSION     "1.6.0"
!define APP_PUBLISHER   "RAPL Group"
!define APP_ID          "Sitekeeper"
!define APP_REGKEY      "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}"

Name "${APP_NAME}"
BrandingText "${APP_NAME} ${APP_VERSION}"
OutFile "Sitekeeper-${APP_VERSION}-Setup.exe"
InstallDir "$PROGRAMFILES64\${APP_NAME}"
InstallDirRegKey HKLM "Software\${APP_ID}" "InstallDir"
RequestExecutionLevel admin
ShowInstDetails show
ShowUnInstDetails show

VIProductVersion "1.6.0.0"
VIAddVersionKey "ProductName"     "${APP_NAME}"
VIAddVersionKey "FileDescription" "${APP_NAME} Setup"
VIAddVersionKey "CompanyName"     "${APP_PUBLISHER}"
VIAddVersionKey "LegalCopyright"  "Copyright (c) 2026 ${APP_PUBLISHER}. Author: Lukas Peterek."
VIAddVersionKey "FileVersion"     "${APP_VERSION}.0"
VIAddVersionKey "ProductVersion"  "${APP_VERSION}.0"

!include "MUI2.nsh"
!include "x64.nsh"
!include "FileFunc.nsh"

!define MUI_ICON   "payload\icon.ico"
!define MUI_UNICON "payload\icon.ico"
!define MUI_ABORTWARNING

; Launching straight from here would hand the app this installer's elevated
; token, and Windows hides mapped network drives (Z:, Y: ...) from elevated
; programs - the app would start blind to every network share while Explorer
; still showed them. Going through Explorer, which runs as the logged-in user,
; starts the app unelevated like a Start-menu click does.
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchAsUser
!define MUI_FINISHPAGE_RUN_TEXT "Launch ${APP_NAME}"

Function LaunchAsUser
  Exec '"$WINDIR\explorer.exe" "$INSTDIR\${APP_EXE}"'
FunctionEnd

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "LICENSE.txt"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; --- close a running instance before (un)installing -----------------------
!macro KillRunning
  nsExec::Exec 'taskkill /F /IM ${APP_EXE}'
  Pop $0
  Sleep 500
!macroend

; --- take over from the MySQL Runner install, if there is one -------------
!define LEGACY_NAME   "MySQL Runner"
!define LEGACY_REGKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\MySQLRunner"

Function RetireLegacyInstall
  ; This app was called MySQL Runner until 1.3.0. Its uninstall entry, its
  ; shortcuts and its own copy of the exe are all still there under the old
  ; name, and nothing about a new install would remove them - so offer.
  ReadRegStr $0 HKLM "${LEGACY_REGKEY}" "UninstallString"
  ${If} $0 == ""
    Return
  ${EndIf}
  MessageBox MB_YESNO|MB_ICONQUESTION \
    "${LEGACY_NAME} is installed. It is now called ${APP_NAME}.$\n$\nRemove \
the old install first? Your saved connections are kept either way - they live \
in your user profile, not in the program folder." \
    IDNO skip
  nsExec::Exec 'taskkill /F /IM MySQLRunner.exe'
  Pop $1
  ReadRegStr $2 HKLM "${LEGACY_REGKEY}" "InstallLocation"
  ${If} $2 != ""
    ExecWait '$0 /S _?=$2'
    ; The uninstaller copies itself out to run; the stub it leaves behind is
    ; only removed once it has finished.
    Delete "$2\Uninstall.exe"
    RMDir "$2"
  ${Else}
    ExecWait '$0 /S'
  ${EndIf}
  skip:
FunctionEnd

Function .onInit
  ; enforce 64-bit host
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "This application requires 64-bit Windows."
    Abort
  ${EndIf}
  SetShellVarContext all
  Call RetireLegacyInstall
FunctionEnd

Section "Sitekeeper (required)" SecMain
  SectionIn RO
  !insertmacro KillRunning

  SetOutPath "$INSTDIR"
  File "payload\${APP_EXE}"
  File "payload\icon.ico"
  File "LICENSE.txt"

  ; Start Menu + Desktop shortcuts
  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortcut  "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\icon.ico" 0
  CreateShortcut  "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk" "$INSTDIR\Uninstall.exe"
  CreateShortcut  "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\icon.ico" 0

  ; Remember install dir
  WriteRegStr HKLM "Software\${APP_ID}" "InstallDir" "$INSTDIR"

  ; Add/Remove Programs entry
  WriteRegStr   HKLM "${APP_REGKEY}" "DisplayName"     "${APP_NAME}"
  WriteRegStr   HKLM "${APP_REGKEY}" "DisplayVersion"  "${APP_VERSION}"
  WriteRegStr   HKLM "${APP_REGKEY}" "Publisher"       "${APP_PUBLISHER}"
  WriteRegStr   HKLM "${APP_REGKEY}" "DisplayIcon"     "$INSTDIR\${APP_EXE}"
  WriteRegStr   HKLM "${APP_REGKEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr   HKLM "${APP_REGKEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegStr   HKLM "${APP_REGKEY}" "InstallLocation" "$INSTDIR"
  WriteRegDWORD HKLM "${APP_REGKEY}" "NoModify" 1
  WriteRegDWORD HKLM "${APP_REGKEY}" "NoRepair" 1

  ; Estimated size (KB)
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKLM "${APP_REGKEY}" "EstimatedSize" "$0"

  WriteUninstaller "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Uninstall"
  SetShellVarContext all
  !insertmacro KillRunning

  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\icon.ico"
  Delete "$INSTDIR\LICENSE.txt"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir  "$INSTDIR"

  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk"
  RMDir  "$SMPROGRAMS\${APP_NAME}"
  Delete "$DESKTOP\${APP_NAME}.lnk"

  DeleteRegKey HKLM "${APP_REGKEY}"
  DeleteRegKey HKLM "Software\${APP_ID}"
  ; Note: user data in %APPDATA%\Sitekeeper is intentionally left intact.
SectionEnd
