$ErrorActionPreference = 'Stop'
$t = Join-Path $env:TEMP ("zk_" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $t | Out-Null
$src = Join-Path $t '한글 폴더'
New-Item -ItemType Directory $src | Out-Null
Set-Content (Join-Path $src '파일 one.txt') ('가나다' * 2000) -Encoding UTF8
[IO.File]::WriteAllBytes((Join-Path $src 'rand.bin'), (& { $b = New-Object byte[] 200000; (New-Object Random).NextBytes($b); ,$b }))
$cli = Join-Path $PSScriptRoot 'yzip_cli.py'
$res = @()

# 1) yzip -> Windows Expand-Archive
python $cli c (Join-Path $t 'a.zip') $src | Out-Null
Expand-Archive (Join-Path $t 'a.zip') (Join-Path $t 'a_out')
$res += "yzip->Windows : " + ((Get-FileHash (Join-Path $t 'a_out\한글 폴더\rand.bin')).Hash -eq (Get-FileHash (Join-Path $src 'rand.bin')).Hash)

# 2) Windows Compress-Archive -> yzip
Compress-Archive -Path $src -DestinationPath (Join-Path $t 'w.zip')
python $cli t (Join-Path $t 'w.zip') | Out-Null; $ok1 = ($LASTEXITCODE -eq 0)
python $cli x (Join-Path $t 'w.zip') -o (Join-Path $t 'w_out') | Out-Null
$res += "Windows->yzip : " + ($ok1 -and ((Get-FileHash (Join-Path $t 'w_out\한글 폴더\rand.bin')).Hash -eq (Get-FileHash (Join-Path $src 'rand.bin')).Hash))

# 3) tar.exe (bsdtar, ships with Windows) reads yzip zip
New-Item -ItemType Directory (Join-Path $t 't_out') | Out-Null
tar -xf (Join-Path $t 'a.zip') -C (Join-Path $t 't_out')
$res += "yzip->tar.exe  : " + ((Get-FileHash (Get-ChildItem (Join-Path $t 't_out') -Recurse -Filter rand.bin).FullName).Hash -eq (Get-FileHash (Join-Path $src 'rand.bin')).Hash)

# 4) 7-Zip / Bandizip reads yzip AES zip, if installed
python $cli c (Join-Path $t 'e.zip') $src -p 'p@ss한글' | Out-Null
$sz = 'C:\Program Files\7-Zip\7z.exe'
if (Test-Path $sz) {
  & $sz t (Join-Path $t 'e.zip') '-pp@ss한글' | Out-Null
  $res += "yzip AES->7-Zip: " + ($LASTEXITCODE -eq 0)
} else {
  $bz = 'C:\Program Files\Bandizip\bz.exe'
  $pr = Start-Process $bz -ArgumentList 't','-y',"-p:p@ss한글",(Join-Path $t 'e.zip') -PassThru -WindowStyle Hidden; if (-not $pr.WaitForExit(30000)) { $pr.Kill(); $LASTEXITCODE = 99 } else { $LASTEXITCODE = $pr.ExitCode }
  $res += "yzip AES->Bandizip bz.exe (exit $LASTEXITCODE): " + ($LASTEXITCODE -eq 0)
}

# 5) performance: 300MB incompressible-ish file
$big = Join-Path $t 'big.bin'
$fs = [IO.File]::Create($big); $buf = New-Object byte[] 1048576; (New-Object Random).NextBytes($buf)
1..300 | ForEach-Object { $fs.Write($buf, 0, $buf.Length) }; $fs.Close()
$sw = [Diagnostics.Stopwatch]::StartNew()
python $cli c (Join-Path $t 'big.zip') $big -l 1 | Out-Null
$res += "300MB compress (-l1): {0:N1}s" -f $sw.Elapsed.TotalSeconds
$sw.Restart()
python $cli t (Join-Path $t 'big.zip') | Out-Null
$res += "300MB test: {0:N1}s" -f $sw.Elapsed.TotalSeconds

$res
Remove-Item $t -Recurse -Force
