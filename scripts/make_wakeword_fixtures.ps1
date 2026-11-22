Add-Type -AssemblyName System.Speech
$dir = Join-Path $PSScriptRoot '..	estsixtures\wakeword'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)

$phrases = @(
  @{ n='pos_hey_jarvis';        t='Hey Jarvis' },
  @{ n='pos_hey_jarvis_cpu';    t='Hey Jarvis, what is my CPU usage?' },
  @{ n='pos_hey_jarvis_there';  t='Hey Jarvis. Are you there?' },
  @{ n='pos_jarvis_bare';       t='Jarvis' },
  @{ n='neg_hey_google';        t='Hey Google, what is the weather?' },
  @{ n='neg_cpu';               t='What is my CPU usage?' },
  @{ n='neg_travis';            t='The service was marvellous, and Travis agreed.' },
  @{ n='neg_drive_us';          t='Hey, drive us to the shop.' },
  @{ n='neg_long';              t='I was reading about the history of the printing press this afternoon.' }
)
foreach ($voice in @('Microsoft David Desktop','Microsoft Zira Desktop')) {
  $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
  $s.SelectVoice($voice)
  $tag = $voice.Split(' ')[1]
  foreach ($p in $phrases) {
    $path = Join-Path $dir ("{0}__{1}.wav" -f $p.n, $tag)
    $s.SetOutputToWaveFile($path, $fmt)
    $s.Speak($p.t)
  }
  $s.SetOutputToNull()
  $s.Dispose()
}
Get-ChildItem $dir -Filter *.wav | Measure-Object | ForEach-Object { "wrote $($_.Count) wav files to $dir" }
