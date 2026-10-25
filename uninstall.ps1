# Removes the browser registration and shortcuts. Your downloads and %APPDATA%\YDM settings are kept.
$hostId = "com.ydm.host"
foreach ($b in "Google\Chrome", "Microsoft\Edge", "BraveSoftware\Brave-Browser", "Chromium", "Vivaldi", "Opera Software") {
    Remove-Item -Force -ErrorAction SilentlyContinue "HKCU:\Software\$b\NativeMessagingHosts\$hostId"
}
Remove-ItemProperty -ErrorAction SilentlyContinue -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "YDM"
Remove-Item -ErrorAction SilentlyContinue (Join-Path ([Environment]::GetFolderPath("Programs")) "YDM.lnk")
Write-Host "YDM unregistered. Remove the extension from chrome://extensions yourself."
