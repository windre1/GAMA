<#
.SYNOPSIS
    Build installer GAMA lalu unggah ke GitHub Releases supaya tombol Update
    di aplikasi user langsung melihatnya.

.DESCRIPTION
    Alurnya:
      1. Baca APP_VERSION dari gama_update.py  (satu-satunya sumber versi)
      2. Compile installer:  ISCC /DMyAppVersion=<versi> setup.iss
      3. Buat rilis di GitHub (tag v<versi>) lalu unggah installer sebagai aset
      4. Aplikasi user otomatis melihatnya lewat /repos/.../releases/latest

    TIDAK perlu gh CLI - memakai GitHub REST API langsung.

.PARAMETER Token
    GitHub Personal Access Token dengan scope "repo" (atau "public_repo" untuk
    repo publik). Kalau tidak diisi, dibaca dari environment GITHUB_TOKEN.
    JANGAN menaruh token di dalam file ini - file ini bisa ikut terdistribusi.

.PARAMETER SkipBuild
    Lewati compile, langsung unggah installer yang sudah ada di installer_output.

.PARAMETER Notes
    Catatan rilis yang tampil di modal Update aplikasi.

.EXAMPLE
    $env:GITHUB_TOKEN = "ghp_xxx"
    .\publish_update.ps1 -Notes "Perbaikan upload thumbnail YouTube"

.EXAMPLE
    .\publish_update.ps1 -SkipBuild -Notes "Rilis ulang"
#>

[CmdletBinding()]
param(
    [string]$Token = $env:GITHUB_TOKEN,
    [switch]$SkipBuild,
    [string]$Notes = "",
    [switch]$Draft
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# Token boleh ditaruh di file .github_token (satu baris, sudah masuk .gitignore)
# supaya tidak perlu diketik ulang tiap rilis dan tidak nyangkut di riwayat shell.
if (-not $Token) {
    $tf = Join-Path $PSScriptRoot '.github_token'
    if (Test-Path $tf) { $Token = (Get-Content $tf -Raw).Trim() }
}

function W-Step($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }

# Windows PowerShell 5.1 mengirim -Body bertipe string memakai ISO-8859-1.
# Catatan rilis yang memuat karakter non-ASCII (tanda pisah panjang, tanda
# kutip miring, emoji) jadi rusak di tengah jalan dan GitHub menolaknya dengan
# 400 "Problems parsing JSON" — sudah terjadi saat merilis 1.1.8. Mengirim
# sebagai byte UTF-8 membuat encoding-nya pasti, bukan bergantung locale.
#
# Koma di depan WAJIB. Tanpa itu PowerShell membongkar array saat fungsi
# mengembalikan nilai, sehingga byte[] berubah jadi Object[] dan
# Invoke-RestMethod mengirimnya sebagai array angka JSON ([123,34,...]).
# GitHub lalu menolak dengan 422 "123 is not an object".
function ToUtf8Body($json) { , [System.Text.Encoding]::UTF8.GetBytes($json) }
function W-Ok($t)   { Write-Host "    [OK]    $t" -ForegroundColor Green }
function W-Bad($t)  { Write-Host "    [GAGAL] $t" -ForegroundColor Red }
function W-Info($t) { Write-Host "            $t" -ForegroundColor Gray }

# ── 1. Baca versi & repo dari gama_update.py ──────────────────────────────
W-Step "Membaca versi dari gama_update.py"
$src = Get-Content "gama_update.py" -Raw

$verMatch = [regex]::Match($src, 'APP_VERSION\s*=\s*"([^"]+)"')
if (-not $verMatch.Success) { W-Bad "APP_VERSION tidak ditemukan di gama_update.py"; exit 1 }
$version = $verMatch.Groups[1].Value

$ownerMatch = [regex]::Match($src, 'GITHUB_OWNER\s*=\s*"([^"]+)"')
$repoMatch  = [regex]::Match($src, 'GITHUB_REPO\s*=\s*"([^"]+)"')
$owner = $ownerMatch.Groups[1].Value
$repo  = $repoMatch.Groups[1].Value

W-Ok "versi : $version"
W-Ok "repo  : $owner/$repo"

if ($owner -like 'GANTI*' -or $repo -like 'GANTI*') {
    W-Bad "GITHUB_OWNER / GITHUB_REPO belum diisi di gama_update.py"
    W-Info "Isi dulu dengan username & nama repo GitHub Anda (repo harus PUBLIK)."
    exit 1
}

if (-not $Token) {
    W-Bad "Token GitHub belum ada."
    W-Info 'Buat di https://github.com/settings/tokens (scope: public_repo), lalu:'
    W-Info '  $env:GITHUB_TOKEN = "ghp_xxx"'
    exit 1
}

# ── 2. Compile installer ──────────────────────────────────────────────────
$installer = Join-Path "installer_output" "GAMA-Setup-$version.exe"

if ($SkipBuild) {
    W-Step "Lewati compile (-SkipBuild)"
} else {
    W-Step "Compile installer (versi $version)"
    $iscc = $null
    foreach ($p in @(
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
        if (Test-Path $p) { $iscc = $p; break }
    }
    if (-not $iscc) {
        $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($cmd) { $iscc = $cmd.Source }
    }
    if (-not $iscc) { W-Bad "ISCC.exe (Inno Setup 6) tidak ditemukan"; exit 1 }
    W-Info $iscc

    & $iscc "/DMyAppVersion=$version" "setup.iss"
    if ($LASTEXITCODE -ne 0) { W-Bad "Compile installer gagal"; exit 1 }
}

if (-not (Test-Path $installer)) {
    W-Bad "Installer tidak ada: $installer"
    exit 1
}

# ── Penjaga: installer WAJIB lebih baru dari seluruh isi dist ─────────────
# PERNAH TERJADI: publish dijalankan saat PyInstaller belum benar-benar
# selesai. Installer terbentuk dari dist versi LAMA, terunggah dengan label
# versi baru, dan aplikasi hasil update melaporkan diri sebagai versi lama —
# sehingga menawarkan update yang sama berulang tanpa henti.
# Membaca log build tidak cukup; yang bisa dipercaya adalah timestamp file.
W-Step "Memastikan installer dibuat dari build terbaru"
$distDir = "dist\GAMA"
if (Test-Path $distDir) {
    $waktuInstaller = (Get-Item $installer).LastWriteTime
    $lebihBaru = Get-ChildItem $distDir -Recurse -File -ErrorAction SilentlyContinue |
                 Where-Object { $_.LastWriteTime -gt $waktuInstaller }
    if ($lebihBaru) {
        W-Bad ("{0} file di dist LEBIH BARU dari installer." -f $lebihBaru.Count)
        W-Info "Installer dibuat sebelum build selesai - isinya versi lama."
        W-Info "Tunggu build tuntas, hapus installer itu, lalu jalankan lagi:"
        W-Info "  Remove-Item '$installer'"
        W-Info "contoh file yang tertinggal:"
        $lebihBaru | Select-Object -First 3 | ForEach-Object { W-Info ("  " + $_.FullName) }
        exit 1
    }
    W-Ok "installer lebih baru dari semua file dist"
} else {
    W-Info "folder dist tidak ada - lewati pemeriksaan"
}

$size = (Get-Item $installer).Length
W-Ok ("installer siap: {0} ({1:N1} MB)" -f $installer, ($size / 1MB))

# ── 3. Buat rilis di GitHub ───────────────────────────────────────────────
$headers = @{
    Authorization          = "Bearer $Token"
    Accept                 = "application/vnd.github+json"
    'X-GitHub-Api-Version' = '2022-11-28'
    'User-Agent'           = 'GAMA-Publisher'
}
$apiBase = "https://api.github.com/repos/$owner/$repo"

# GitHub tidak bisa membuat tag rilis di repo yang BELUM punya commit sama
# sekali. Repo rilis biasanya memang kosong, jadi commit pertama (README)
# dibuat di sini lewat Contents API - tidak perlu git push, tidak ada risiko
# source ikut terkirim.
W-Step "Memastikan repo punya commit"
$adaCommit = $true
try {
    Invoke-RestMethod -Uri "$apiBase/commits?per_page=1" -Headers $headers -Method Get | Out-Null
} catch {
    $adaCommit = $false
}
if (-not $adaCommit) {
    W-Info "repo masih kosong - membuat README.md"
    $readme = @"
# GAMA

Google Account Manager & Automation.

Unduh versi terbaru di tab **[Releases](https://github.com/$owner/$repo/releases/latest)**.

Aplikasi GAMA yang sudah terpasang bisa memperbarui dirinya sendiri:
buka GAMA lalu klik tombol **Update**.

Butuh bantuan? Gabung grup support:
https://chat.whatsapp.com/JNXX95bgJbX6cbvGRLzbSK

---
Repositori ini hanya menyimpan berkas rilis. Kode sumber tidak dipublikasikan.
"@
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($readme))
    $body = @{ message = "Init repo rilis GAMA"; content = $b64 } | ConvertTo-Json
    Invoke-RestMethod -Uri "$apiBase/contents/README.md" -Headers $headers -Method Put -Body (ToUtf8Body $body) -ContentType 'application/json; charset=utf-8' | Out-Null
    W-Ok "README.md dibuat - repo siap menerima rilis"
} else {
    W-Ok "repo sudah punya commit"
}

W-Step "Membuat rilis v$version di GitHub"

# Kalau tag sudah pernah dirilis, pakai rilis itu (jangan bikin ganda)
$release = $null
try {
    $release = Invoke-RestMethod -Uri "$apiBase/releases/tags/v$version" -Headers $headers -Method Get
    W-Info "rilis v$version sudah ada, memakai yang lama"
} catch {
    $body = @{
        tag_name = "v$version"
        name     = "GAMA $version"
        body     = if ($Notes) { $Notes } else { "Rilis GAMA $version" }
        draft    = [bool]$Draft
        prerelease = $false
    } | ConvertTo-Json
    $release = Invoke-RestMethod -Uri "$apiBase/releases" -Headers $headers -Method Post -Body (ToUtf8Body $body) -ContentType 'application/json; charset=utf-8'
    W-Ok "rilis dibuat: $($release.html_url)"
}

# Hapus aset lama dengan nama sama supaya unggahan tidak ditolak
foreach ($a in $release.assets) {
    if ($a.name -eq (Split-Path $installer -Leaf)) {
        W-Info "menghapus aset lama: $($a.name)"
        Invoke-RestMethod -Uri "$apiBase/releases/assets/$($a.id)" -Headers $headers -Method Delete | Out-Null
    }
}

# ── 4. Unggah installer ───────────────────────────────────────────────────
W-Step "Mengunggah installer (ini yang lama - sabar)"
$uploadUrl = ($release.upload_url -replace '\{\?name,label\}', '') + "?name=" + [uri]::EscapeDataString((Split-Path $installer -Leaf))

$uploadHeaders = $headers.Clone()
$uploadHeaders['Content-Type'] = 'application/octet-stream'

$mulai = Get-Date
$asset = Invoke-RestMethod -Uri $uploadUrl -Headers $uploadHeaders -Method Post -InFile $installer
$lama = [int]((Get-Date) - $mulai).TotalSeconds
W-Ok "terunggah dalam $lama detik"

# ── 5. Verifikasi seperti yang dilihat aplikasi user ──────────────────────
W-Step "Verifikasi dari sisi aplikasi"
$latest = Invoke-RestMethod -Uri "$apiBase/releases/latest" -Headers @{ 'User-Agent' = 'GAMA-Publisher' } -Method Get
$aset = $latest.assets | Where-Object { $_.name -like 'GAMA-Setup-*.exe' } | Select-Object -First 1
Write-Host ""
Write-Host ("    tag        : {0}" -f $latest.tag_name)
Write-Host ("    aset       : {0}" -f $aset.name)
Write-Host ("    ukuran     : {0:N1} MB" -f ($aset.size / 1MB))
Write-Host ("    unduhan    : {0}" -f $aset.browser_download_url)
Write-Host ""
if ($aset.size -ne $size) {
    W-Bad "Ukuran di GitHub ($($aset.size)) beda dengan file lokal ($size)."
    W-Info "Unggahan mungkin terpotong - jalankan ulang dengan -SkipBuild."
    exit 1
}
W-Ok "Ukuran cocok. Tombol Update di aplikasi user sudah melihat versi $version."

# ── 6. Sync ke folder install lokal (jika ada) ───────────────────────────
# Ini mencegah masalah di mana installer tidak meng-overwrite file yang
# terkunci saat GAMA sedang berjalan — file di folder install langsung
# di-sync dari dist\ setelah upload berhasil.
$installDir = "$env:LOCALAPPDATA\Programs\GAMA - Google Account Manager & Automation\GAMA"
$distDir    = Join-Path $PSScriptRoot "dist\GAMA"
if ((Test-Path $installDir) -and (Test-Path $distDir)) {
    W-Step "Sync dist\ ke folder install lokal"
    $gamaProc = Get-Process -Name "GAMA" -ErrorAction SilentlyContinue
    if ($gamaProc) {
        W-Info "GAMA sedang berjalan (PID $($gamaProc.Id)) - skip sync otomatis."
        W-Info "Tutup GAMA lalu copy manual: Copy-Item '$distDir\*' '$installDir\' -Recurse -Force"
    } else {
        Copy-Item -Path "$distDir\*" -Destination $installDir -Recurse -Force
        W-Ok "Sync selesai -> $installDir"
    }
} else {
    W-Info "Folder install lokal tidak ditemukan - skip sync."
}
Write-Host ""
