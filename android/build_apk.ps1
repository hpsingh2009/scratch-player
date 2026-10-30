$ErrorActionPreference = "Stop"

$sdkDir = "$env:LOCALAPPDATA\Android\Sdk-lite"
$btDir = (Get-ChildItem "$sdkDir\build-tools" -Directory | Select-Object -First 1).FullName
$platDir = (Get-ChildItem "$sdkDir\platform" -Directory | Select-Object -First 1).FullName
$jdkDir = (Get-ChildItem "$sdkDir\jdk" -Directory | Select-Object -First 1).FullName

$env:JAVA_HOME = $jdkDir
$env:PATH = "$jdkDir\bin;$btDir;$env:PATH"

$aapt2 = "$btDir\aapt2.exe"
$zipalign = "$btDir\zipalign.exe"
$d8Jar = "$btDir\lib\d8.jar"
$apksignerJar = "$btDir\lib\apksigner.jar"
$androidJar = "$platDir\android.jar"
$javac = "$jdkDir\bin\javac.exe"
$java = "$jdkDir\bin\java.exe"
$keytool = "$jdkDir\bin\keytool.exe"

$projDir = (Split-Path -Parent $PSScriptRoot)
if (-not $projDir) { $projDir = (Resolve-Path "$PSScriptRoot\..").Path }
$androidDir = $PSScriptRoot
$buildDir = "$androidDir\build"
$assetsDir = "$androidDir\assets"

if (Test-Path $buildDir) { Remove-Item $buildDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path "$buildDir\gen" | Out-Null
New-Item -ItemType Directory -Force -Path "$buildDir\obj" | Out-Null
New-Item -ItemType Directory -Force -Path "$buildDir\dex" | Out-Null
New-Item -ItemType Directory -Force -Path $assetsDir | Out-Null

# 1. Copy latest web app assets
Copy-Item "$projDir\index.html" "$assetsDir\index.html" -Force
Copy-Item "$projDir\desktop-bridge.js" "$assetsDir\desktop-bridge.js" -Force

# 2. Compile resources with aapt2
& $aapt2 compile --dir "$androidDir\res" -o "$buildDir\res.zip"
if ($LASTEXITCODE -ne 0) { throw "aapt2 compile failed" }

# 3. Link resources and assets into unaligned APK + generate R.java
& $aapt2 link `
  -I $androidJar `
  --manifest "$androidDir\AndroidManifest.xml" `
  -A $assetsDir `
  --java "$buildDir\gen" `
  -o "$buildDir\unaligned.apk" `
  "$buildDir\res.zip"
if ($LASTEXITCODE -ne 0) { throw "aapt2 link failed" }

# 4. Compile Java sources (generated R.java + all app sources: MainActivity, PlaybackService, ...)
$javaFiles = @()
$javaFiles += Get-ChildItem "$buildDir\gen" -Recurse -Filter "*.java" | ForEach-Object { $_.FullName }
$javaFiles += Get-ChildItem "$androidDir\src" -Recurse -Filter "*.java" | ForEach-Object { $_.FullName }
& $javac --release 11 -cp $androidJar -d "$buildDir\obj" $javaFiles
if ($LASTEXITCODE -ne 0) { throw "javac failed" }

# 5. Convert .class files to classes.dex using d8
$classFiles = Get-ChildItem "$buildDir\obj" -Recurse -Filter "*.class" | ForEach-Object { $_.FullName }
& $java -cp $d8Jar com.android.tools.r8.D8 --lib $androidJar --min-api 24 --output "$buildDir\dex" $classFiles
if ($LASTEXITCODE -ne 0) { throw "d8 failed" }

# 6. Add classes.dex to the root of unaligned.apk
$jar = "$jdkDir\bin\jar.exe"
& $jar -uf "$buildDir\unaligned.apk" -C "$buildDir\dex" classes.dex
if ($LASTEXITCODE -ne 0) { throw "jar uf classes.dex failed" }

# 7. Align the APK with zipalign
& $zipalign -f -p 4 "$buildDir\unaligned.apk" "$buildDir\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw "zipalign failed" }

# 8. Generate signing keystore if not present and sign APK (v1 + v2 + v3)
$keystore = "$androidDir\release.keystore"
if (!(Test-Path $keystore)) {
  & $keytool -genkeypair -v `
    -keystore $keystore `
    -alias mp3player `
    -keyalg RSA `
    -keysize 2048 `
    -validity 10000 `
    -storepass android123 `
    -keypass android123 `
    -dname "CN=MP3 Player, OU=Music, O=MP3Player, L=City, S=State, C=US"
}

$outApk = "$projDir\MP3-Player.apk"
& $java -jar $apksignerJar sign `
  --ks $keystore `
  --ks-key-alias mp3player `
  --ks-pass pass:android123 `
  --key-pass pass:android123 `
  --out $outApk `
  "$buildDir\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw "apksigner sign failed" }

& $java -jar $apksignerJar verify --verbose $outApk
if ($LASTEXITCODE -ne 0) { throw "apksigner verify failed" }

Write-Host "APK_BUILD_SUCCESS: $outApk"
Get-Item $outApk | Select-Object FullName, Length, LastWriteTime
