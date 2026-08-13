; setup.iss — Installer GAMA (Google Account Manager & Automation)
;
; Urutan build:
;   1. pyinstaller gama.spec --noconfirm --clean     -> hasil di dist\GAMA\
;   2. Buka file ini di Inno Setup Compiler, klik Compile
;      (atau lewat CLI: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" setup.iss)
;   3. Installer jadi ada di installer_output\GAMA-Setup-<versi>.exe
;
; Kenapa install ke {localappdata}\Programs (per-user, BUKAN Program Files):
; GAMA nulis banyak data runtime (data/, downloads/, output/, appdata engine
; YT Uploader/TEKOTOK) tepat di sebelah GAMA.exe (lihat BASE_DIR frozen-mode
; di main.py) — Program Files butuh admin & sering bentrok sama write biasa.
; Per-user install (tanpa admin) menghindari masalah itu sama sekali.

#define MyAppName "GAMA - Google Account Manager & Automation"
; Versi bisa dioper dari script build:  ISCC /DMyAppVersion=1.1.3 setup.iss
; Sumber kebenarannya APP_VERSION di gama_update.py — publish_update.ps1
; membacanya dari sana supaya versi aplikasi & installer tidak pernah beda.
; Nilai di bawah hanya cadangan kalau ISCC dijalankan manual tanpa /D.
#ifndef MyAppVersion
  #define MyAppVersion "1.1.2"
#endif
#define MyAppPublisher "GAMA"
#define MyAppExeName "GAMA.exe"
#define MyAppIconFile "assets\logo.ico"

[Setup]
; GUID tetap — JANGAN diganti antar rilis (dipakai Windows utk kenali "app yang sama"
; saat update/uninstall). Kalau diganti, upgrade akan dianggap app baru yang terpisah.
AppId={{7C1E9C7A-6A2B-4C6E-9B2D-5D3F7B2C4E11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppSupportURL=https://chat.whatsapp.com/JNXX95bgJbX6cbvGRLzbSK
DefaultDirName={localappdata}\Programs\GAMA
DefaultGroupName=GAMA
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=installer_output
OutputBaseFilename=GAMA-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
SetupIconFile={#MyAppIconFile}
UninstallDisplayIcon={app}\{#MyAppExeName}
WizardStyle=modern
; GAMA.exe + worker-worker-nya (facebook_bridge.exe dkk) ditutup otomatis (kalau
; lagi jalan) sebelum install/uninstall, supaya file-nya tidak "in use"/kepending.
CloseApplications=yes
; CloseApplicationsFilter adalah daftar POLA BERKAS yang diperiksa sedang
; dipakai, BUKAN daftar aplikasi yang ditutup. Isinya dulu nama-nama .exe,
; sehingga berkas .pyd (ekstensi Python hasil kompilasi) tidak pernah ikut
; diperiksa. Akibatnya Restart Manager tidak tahu ada proses yang memegangnya,
; aplikasinya tidak ditutup, lalu instalasi berhenti dengan
; "DeleteFile failed; code 5. Access is denied." — persis yang terjadi saat
; update ke 1.1.8. Yang benar: pola berkas, dan .pyd wajib masuk.
CloseApplicationsFilter=*.exe,*.dll,*.pyd,*.chm
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Buat shortcut di Desktop"; GroupDescription: "Shortcut tambahan:"; Flags: unchecked

[Files]
; Seluruh isi dist\GAMA (GAMA.exe + semua worker exe + _internal + engines/ + assets)
; ikut, apa adanya — folder itu SUDAH bersih dari data sensitif (lihat gama.spec,
; datas cuma whitelist templates/static/assets/engines, TIDAK ADA data/ atau tools/).
Source: "dist\GAMA\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs restartreplace uninsrestartdelete

[Icons]
Name: "{group}\GAMA"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\Uninstall GAMA"; Filename: "{uninstallexe}"
Name: "{autodesktop}\GAMA"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Jalankan GAMA sekarang"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; SENGAJA TIDAK menghapus data\, downloads\, output\, appdata\ (dibuat GAMA
; sendiri saat jalan) — itu akun/lisensi/history milik user, jangan hilang
; cuma karena uninstall (mis. mau reinstall versi baru). Kalau user memang
; mau bersih total, hapus manual folder install-nya sendiri.

[Code]
// Hitung jumlah subfolder (= jumlah profil akun) di dalam data\google_profiles,
// dipakai buat kasih tahu user secara eksplisit bahwa data lama aman.
function CountSubdirs(const Dir: String): Integer;
var
  FindRec: TFindRec;
begin
  Result := 0;
  if FindFirst(Dir + '\*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0)
           and (FindRec.Name <> '.') and (FindRec.Name <> '..') then
          Result := Result + 1;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

// Deteksi Chrome.
//
// Versi lama memakai {autopf} dan {autopf32}. Itu SALAH untuk installer ini:
// karena PrivilegesRequired=lowest, konstanta "auto" milik Inno menunjuk ke
// varian NON-ADMIN, yaitu %LOCALAPPDATA%\Programs — bukan C:\Program Files.
// Akibatnya Chrome yang dipasang untuk semua user (lokasi paling umum,
// C:\Program Files\Google\Chrome) tidak pernah diperiksa, dan installer
// memunculkan peringatan "Chrome tidak terdeteksi" padahal Chrome jelas ada.
//
// Sekarang: registry App Paths lebih dulu (sumber paling sahih, dipakai
// Windows sendiri untuk menemukan chrome.exe), baru daftar folder eksplisit
// dengan konstanta {commonpf}/{commonpf32} yang benar-benar menunjuk ke
// C:\Program Files dan C:\Program Files (x86).
function ChromeDariRegistry(): String;
var
  Kunci, Nilai: String;
begin
  // Pascal Script Inno tidak mengenal blok const di dalam fungsi, jadi
  // kuncinya ditaruh sebagai variabel biasa.
  Kunci := 'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe';
  Result := '';
  if RegQueryStringValue(HKEY_LOCAL_MACHINE, Kunci, '', Nilai) and (Nilai <> '') then
    Result := RemoveQuotes(Nilai)
  else if RegQueryStringValue(HKEY_CURRENT_USER, Kunci, '', Nilai) and (Nilai <> '') then
    Result := RemoveQuotes(Nilai);
end;

function ChromeTerpasang(): Boolean;
var
  DariReg: String;
  i: Integer;
  Kandidat: array[0..4] of String;
begin
  DariReg := ChromeDariRegistry();
  if (DariReg <> '') and FileExists(DariReg) then
  begin
    // Dicatat ke log instalasi supaya kalau ada user mengeluh "Chrome tidak
    // terdeteksi" lagi, jalur mana yang diperiksa bisa langsung dilihat.
    Log('[CHROME] Ditemukan lewat registry: ' + DariReg);
    Result := True;
    exit;
  end;
  Log('[CHROME] Registry tidak memberi path yang valid, cek folder...');

  Kandidat[0] := ExpandConstant('{commonpf}\Google\Chrome\Application\chrome.exe');
  Kandidat[1] := ExpandConstant('{commonpf32}\Google\Chrome\Application\chrome.exe');
  Kandidat[2] := ExpandConstant('{localappdata}\Google\Chrome\Application\chrome.exe');
  Kandidat[3] := ExpandConstant('{localappdata}\Programs\Google\Chrome\Application\chrome.exe');
  Kandidat[4] := ExpandConstant('{sd}\Program Files\Google\Chrome\Application\chrome.exe');

  Result := False;
  for i := 0 to 4 do
    if FileExists(Kandidat[i]) then
    begin
      Log('[CHROME] Ditemukan di: ' + Kandidat[i]);
      Result := True;
      exit;
    end;
  Log('[CHROME] TIDAK ditemukan di registry maupun kelima folder yang dicek.');
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not ChromeTerpasang() then
  begin
    // SuppressibleMsgBox (bukan MsgBox biasa) - MsgBox biasa TETAP nongol dan
    // nge-block walau /VERYSILENT /SUPPRESSMSGBOXES dipakai (mis. install via
    // script/IT push), cuma SuppressibleMsgBox yang beneran taat ke flag itu.
    SuppressibleMsgBox('Google Chrome tidak terdeteksi di komputer ini.' + #13#10 + #13#10 +
      'GAMA butuh Google Chrome terpasang untuk semua fitur otomasi browser ' +
      '(Upload, Downloader, Riset, dll). Instalasi akan tetap lanjut, tapi ' +
      'silakan pasang Chrome dulu dari google.com/chrome sebelum memakai GAMA.',
      mbInformation, MB_OK, IDOK);
  end;
end;

// Pengaman kedua setelah CloseApplicationsFilter: paksa tutup sisa proses GAMA
// tepat sebelum berkas disalin.
//
// Restart Manager bawaan Inno kadang tidak menangkap semuanya — worker bridge
// bisa saja baru dijalankan GAMA sesudah pemeriksaan berlangsung, atau berjalan
// tanpa jendela sehingga tidak terlihat. Kalau satu saja masih hidup, berkas
// .pyd yang sedang dipakainya tidak bisa ditimpa dan instalasi berhenti dengan
// "DeleteFile failed; code 5".
procedure TutupSisaProsesGama();
var
  Kode: Integer;
begin
  Exec(ExpandConstant('{cmd}'),
       '/C taskkill /F /IM GAMA.exe /IM yt_bridge.exe /IM tiktok_bridge.exe' +
       ' /IM facebook_bridge.exe /IM instagram_bridge.exe /IM social_bridge.exe' +
       ' /IM social_downloader_bridge.exe /IM shopee_adb_bridge.exe' +
       ' /IM gmaps_bridge.exe /IM metadata_cleaner_bridge.exe' +
       ' /IM video_generator_bridge.exe /IM voice_over_bridge.exe >nul 2>&1',
       '', SW_HIDE, ewWaitUntilTerminated, Kode);
  // Beri jeda supaya Windows sempat melepas kunci berkasnya.
  Sleep(1200);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  TutupSisaProsesGama();
  Result := '';
end;

// {app} BELUM valid di dalam InitializeSetup (baru resolve setelah wizard page
// pemilihan folder selesai) - ExpandConstant('{app}') di situ malah exception
// "attempt was made to expand the app constant before it was initialized".
// ssInstall (tepat sebelum [Files] mulai disalin) adalah titik paling awal yang
// aman buat baca {app}, makanya deteksi data lama dipindah ke sini.
procedure CurStepChanged(CurStep: TSetupStep);
var
  ProfilesDir: String;
  ProfileCount: Integer;
begin
  if CurStep = ssInstall then
  begin
    // AppId tetap (lihat [Setup] di atas) bikin Inno Setup otomatis mengarahkan
    // {app} ke folder instalasi LAMA kalau GAMA memang sudah pernah dipasang di
    // komputer ini (UsePreviousAppDir bawaan Inno = yes).
    ProfilesDir := ExpandConstant('{app}\data\google_profiles');
    if DirExists(ProfilesDir) then
    begin
      ProfileCount := CountSubdirs(ProfilesDir);
      SuppressibleMsgBox('Instalasi GAMA sebelumnya terdeteksi di folder ini' + #13#10 +
        '(' + IntToStr(ProfileCount) + ' profil akun tersimpan).' + #13#10 + #13#10 +
        'Setup ini HANYA memperbarui file aplikasi (GAMA.exe dan pendukungnya). ' +
        'Semua profil, password, grup, tag, dan label yang sudah tersimpan di ' +
        '"data\" TIDAK akan dihapus atau diubah.',
        mbInformation, MB_OK, IDOK);
    end;
  end;
end;
