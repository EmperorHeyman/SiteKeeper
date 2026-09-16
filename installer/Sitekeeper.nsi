; Sitekeeper - NSIS installer
; Modern UI 2, per-machine install to Program Files (64-bit).

Unicode true
SetCompressor /SOLID lzma

!define APP_NAME        "Sitekeeper"
!define APP_EXE         "Sitekeeper.exe"
!define MCP_EXE         "sitekeeper-mcp.exe"
!define APP_VERSION     "1.13.0"
!define APP_PUBLISHER   "RAPL Group, s.r.o."
!define APP_ID          "Sitekeeper"
!define APP_REGKEY      "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}"
; Handover from a hosting control panel: a URL scheme and a file type,
; both of which hand the app one argument. See storage/provisioning.py.
!define URL_SCHEME      "sitekeeper"
!define CLAIM_EXT       ".skc"
!define CLAIM_PROGID    "Sitekeeper.Claim"
!define CLAIM_MIME      "application/vnd.sitekeeper.claim+json"

Name "${APP_NAME}"
BrandingText "${APP_NAME} ${APP_VERSION}"
OutFile "Sitekeeper-${APP_VERSION}-Setup.exe"
InstallDir "$PROGRAMFILES64\${APP_NAME}"
InstallDirRegKey HKLM "Software\${APP_ID}" "InstallDir"
RequestExecutionLevel admin
ShowInstDetails show
ShowUnInstDetails show

VIProductVersion "1.13.0.0"
VIAddVersionKey "ProductName"     "${APP_NAME}"
VIAddVersionKey "FileDescription" "${APP_NAME} Setup"
VIAddVersionKey "CompanyName"     "${APP_PUBLISHER}"
VIAddVersionKey "LegalCopyright"  "Copyright (c) 2026 ${APP_PUBLISHER} Author: Lukas Peterek."
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
  ; The MCP server: a console build, because the app itself is windowed
  ; and so cannot speak a protocol that lives on stdin and stdout.
  File "payload\${MCP_EXE}"
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

  ; --- handover from a hosting provider -----------------------------------
  ; Two ways in, both landing on the same argument. The sitekeeper:// scheme is
  ; the one-click path from a control panel; the .skc file is the same ticket
  ; downloaded, for browsers that have got stricter about custom schemes and
  ; for panels that would rather mail it. Neither carries a credential - what
  ; the app does with the argument is in storage/provisioning.py.
  WriteRegStr HKCR "${URL_SCHEME}" "" "URL:${APP_NAME} Handover"
  WriteRegStr HKCR "${URL_SCHEME}" "URL Protocol" ""
  WriteRegStr HKCR "${URL_SCHEME}\DefaultIcon" "" '"$INSTDIR\${APP_EXE}",0'
  WriteRegStr HKCR "${URL_SCHEME}\shell\open\command" "" '"$INSTDIR\${APP_EXE}" "%1"'

  WriteRegStr HKCR "${CLAIM_EXT}" "" "${CLAIM_PROGID}"
  WriteRegStr HKCR "${CLAIM_EXT}" "Content Type" "${CLAIM_MIME}"
  WriteRegStr HKCR "${CLAIM_EXT}\OpenWithProgids" "${CLAIM_PROGID}" ""
  WriteRegStr HKCR "${CLAIM_PROGID}" "" "${APP_NAME} connection handover"
  WriteRegStr HKCR "${CLAIM_PROGID}\DefaultIcon" "" "$INSTDIR\icon.ico"
  WriteRegStr HKCR "${CLAIM_PROGID}\shell\open\command" "" '"$INSTDIR\${APP_EXE}" "%1"'
  ; Without this Explorer keeps showing the old (or no) association until the
  ; next sign-in, which reads as "the installer did not work".
  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, i 0, i 0)'

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
  Delete "$INSTDIR\${MCP_EXE}"
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

  ; Leaving these behind would point Windows at an exe that is no longer
  ; there, so every handover link would fail with a shell error instead of
  ; the browser offering to install the app again.
  DeleteRegKey HKCR "${URL_SCHEME}"
  DeleteRegKey HKCR "${CLAIM_PROGID}"
  DeleteRegValue HKCR "${CLAIM_EXT}\OpenWithProgids" "${CLAIM_PROGID}"
  ; Only drop the extension itself if it is still ours - another program
  ; may have claimed it since, and taking it back on the way out would
  ; leave the user with a file type nothing opens.
  ReadRegStr $0 HKCR "${CLAIM_EXT}" ""
  ${If} $0 == "${CLAIM_PROGID}"
    DeleteRegKey HKCR "${CLAIM_EXT}"
  ${EndIf}
  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, i 0, i 0)'

  ; Note: user data in %APPDATA%\Sitekeeper is intentionally left intact.
SectionEnd
