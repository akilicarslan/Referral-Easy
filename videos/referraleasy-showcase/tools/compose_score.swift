// Original composition: The Next Right Step. Native GM instruments, offline rendering.
import AVFoundation
import Foundation

struct NoteEvent {
    let at: Double
    let instrument: Int
    let pitch: UInt8
    let velocity: UInt8
    let on: Bool
}
let root = URL(fileURLWithPath: CommandLine.arguments[1])
let engine = AVAudioEngine()
let format = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 2)!
let programs: [UInt8] = [48, 49, 42, 60, 0, 52, 47, 115]
let pans: [Float] = [-0.36, 0.34, -0.12, 0.16, -0.1, 0.4, 0, -0.4]
var samplers: [AVAudioUnitSampler] = []
let reverb = AVAudioUnitReverb()
engine.attach(reverb)
reverb.loadFactoryPreset(.largeHall2)
reverb.wetDryMix = 28
let instrumentMixer = AVAudioMixerNode()
engine.attach(instrumentMixer)
engine.connect(instrumentMixer, to: reverb, format: format)
engine.connect(reverb, to: engine.mainMixerNode, format: format)
let soundbank = URL(fileURLWithPath: "/System/Library/Components/CoreAudio.component/Contents/Resources/gs_instruments.dls")
for i in 0..<programs.count {
    let sampler = AVAudioUnitSampler()
    engine.attach(sampler)
    try sampler.loadSoundBankInstrument(at: soundbank, program: programs[i], bankMSB: UInt8(kAUSampler_DefaultMelodicBankMSB), bankLSB: 0)
    sampler.stereoPan = pans[i] * 100
    engine.connect(sampler, to: instrumentMixer, format: format)
    samplers.append(sampler)
}
var notes: [NoteEvent] = []
func note(_ instrument: Int, _ pitch: Int, _ start: Double, _ duration: Double, _ velocity: Int) {
    guard start < 58.9 else { return }
    notes.append(NoteEvent(at: start, instrument: instrument, pitch: UInt8(pitch), velocity: UInt8(max(1,min(127,velocity))), on: true))
    notes.append(NoteEvent(at: min(59.1,start+duration), instrument: instrument, pitch: UInt8(pitch), velocity: 0, on: false))
}
let beat = 60.0 / 104.0
let bar = beat * 4
// Non-Lux progression: C#m9, Emaj9, Amaj9, F#m9, Bsus2, G#sus4.
let chords = [[49,56,59,63,68], [52,59,63,66,71], [45,52,56,59,64], [42,49,52,56,61], [47,54,61,66,71], [44,51,56,61,68]]
let progression = [0,0,1,1,2,2,3,4,0,1,2,3,4,5,0,2,3,4,0,1,2,3,4,5,0,0]
for b in 0..<26 {
    let start = Double(b) * bar
    let chord = chords[progression[b]]
    let drive = start < 6 ? 0 : start < 18 ? 1 : start < 35 ? 2 : start < 43 ? 3 : 4
    let v = 38 + drive * 9
    // Long bowed ensemble. Paired voicing creates stereo breadth.
    for (j,p) in chord.enumerated() {
        note(j % 2, p, start, bar * 1.05, v + (j == 0 ? 5 : 0))
    }
    note(2, chord[0] - 12, start, bar * 0.98, v + 8)
    if drive >= 1 {
        let pattern = [0,2,1,3,2,4,1,3]
        for step in 0..<8 {
            let t = start + Double(step) * beat / 2
            let p = chord[pattern[step]] + (step % 4 == 3 ? 12 : 0)
            note(1, p, t, beat * 0.36, 39 + drive * 8 + (step % 4 == 0 ? 8 : 0))
            if step % 2 == 0 { note(4, p + 12, t + 0.012, beat * 0.72, 32 + drive * 4) }
        }
    }
    if drive >= 2 {
        note(6, 36 + progression[b] % 3 * 2, start, beat * 1.6, 65 + drive * 6)
        note(7, 40, start + beat * 2.5, beat * 0.4, 36 + drive * 6)
        if b % 2 == 0 {
            for p in [chord[0], chord[1], chord[2]] { note(3,p,start,bar*0.82,48 + drive * 6) }
        }
    }
    if drive >= 3 {
        note(6, 43, start + beat*2, beat*0.8, 58)
        for p in [chord[2]+12,chord[3]+12] { note(5,p,start,bar*0.95,36 + drive*3) }
    }
}
// Dramatic accents align to the authored chapters rather than copying a soundtrack.
for boundary in [18.0,27.0,35.0,43.0,53.0] {
    note(6, 36, boundary, 1.8, 102)
    note(3, 49, boundary, 1.4, 78)
    note(3, 56, boundary, 1.4, 74)
}
for p in [37,49,56,63,68,75] { note(p<49 ? 2 : 0,p,56.5,2.55,p<49 ? 84 : 67) }
note(6,36,56.5,2.4,105)
notes.sort { $0.at == $1.at ? (!$0.on && $1.on) : $0.at < $1.at }
try engine.enableManualRenderingMode(.offline, format: format, maximumFrameCount: 1024)
try engine.start()
let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 1024)!
let output = root.appendingPathComponent(".cache/score-native.caf")
try FileManager.default.createDirectory(at: output.deletingLastPathComponent(), withIntermediateDirectories: true)
let file = try AVAudioFile(forWriting: output, settings: format.settings)
let length: AVAudioFramePosition = 60 * 48000
var next = 0
var attempts = 0
while engine.manualRenderingSampleTime < length {
    let time = Double(engine.manualRenderingSampleTime) / 48000
    while next < notes.count && notes[next].at <= time + 0.0001 {
        let e = notes[next]
        if e.on { samplers[e.instrument].startNote(e.pitch, withVelocity: e.velocity, onChannel: 0) }
        else { samplers[e.instrument].stopNote(e.pitch,onChannel: 0) }
        next += 1
    }
    var count = min(1024, Int(length - engine.manualRenderingSampleTime))
    if next < notes.count {
        let until = Int((notes[next].at - time) * 48000)
        if until > 0 { count = min(count, until) }
    }
    let status = try engine.renderOffline(AVAudioFrameCount(max(1,count)), to: buffer)
    switch status {
    case .success: try file.write(from: buffer); attempts = 0
    case .cannotDoInCurrentContext: attempts += 1
    case .insufficientDataFromInputNode: attempts += 1
    case .error: fatalError("Offline audio render failed")
    @unknown default: fatalError("Unknown audio render status")
    }
    if attempts > 100 { fatalError("Audio engine did not advance") }
}
engine.stop()
print("Original 60-second orchestral score: \(output.path)")
