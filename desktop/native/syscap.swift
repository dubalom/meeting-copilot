// syscap — macOS system-audio capture for Meeting Copilot (no BlackHole needed).
// ScreenCaptureKit (macOS 13+) → mono 16 kHz int16 little-endian PCM on stdout, 640-sample frames.
// stderr: "ready" once capturing, "error: …" on failure. Exits when stdin closes (parent quit).
// Needs the "Screen & System Audio Recording" permission for the app that launches it.
import CoreMedia
import Foundation
import ScreenCaptureKit

setvbuf(stdout, nil, _IONBF, 0)
signal(SIGPIPE, SIG_IGN)

func log(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

final class Capturer: NSObject, SCStreamOutput, SCStreamDelegate {
    private var stream: SCStream?
    private let queue = DispatchQueue(label: "syscap.audio")
    private let target = 16_000.0
    private let frame = 640
    // low-pass FIR downsampler (windowed sinc, cut-off 7 kHz) — a plain average lets 8–16 kHz fold into speech
    private let taps = 95
    private var h = [Float]()
    private var ring = [Float]()
    private var w = 0
    private var m = -1
    private var next = 0.0
    private var hRate = 0.0
    private var out = [Int16]()
    private let debug = ProcessInfo.processInfo.environment["SYSCAP_DEBUG"] != nil
    private var buffersSeen = 0
    private var peak: Float = 0
    // slow automatic gain: system audio is captured at the user's volume level, often quiet
    private var env: Float = 0
    private var gain: Float = 1

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        guard let display = content.displays.first else {
            throw NSError(domain: "syscap", code: 1, userInfo: [NSLocalizedDescriptionKey: "no display"])
        }
        let filter = SCContentFilter(display: display, excludingWindows: [])
        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true
        cfg.excludesCurrentProcessAudio = true
        cfg.sampleRate = 48_000
        cfg.channelCount = 2
        cfg.width = 2 // we only want audio; keep the video side as cheap as possible
        cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        cfg.queueDepth = 6
        let s = SCStream(filter: filter, configuration: cfg, delegate: self)
        try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        try await s.startCapture()
        stream = s
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        log("error: stream stopped: \(error.localizedDescription)")
        exit(3)
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sb.isValid,
              let fmt = CMSampleBufferGetFormatDescription(sb),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(fmt) else { return }
        let asbd = asbdPtr.pointee
        let isFloat = asbd.mFormatFlags & kAudioFormatFlagIsFloat != 0
        let nonInterleaved = asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved != 0
        let channels = Int(max(1, asbd.mChannelsPerFrame))
        let ratio = asbd.mSampleRate / target
        buffersSeen += 1
        if debug && buffersSeen == 1 {
            log("debug: rate=\(asbd.mSampleRate) ch=\(channels) float=\(isFloat) bits=\(asbd.mBitsPerChannel) nonInterleaved=\(nonInterleaved)")
        }
        if debug && buffersSeen % 50 == 0 { log("debug: buffers=\(buffersSeen) peak=\(peak)"); peak = 0 }
        guard isFloat, asbd.mBitsPerChannel == 32 else { return }

        try? sb.withAudioBufferList { abl, _ in
            let buffers = UnsafeMutableAudioBufferListPointer(abl.unsafeMutablePointer)
            if nonInterleaved {
                let n = Int(buffers[0].mDataByteSize) / 4
                let ptrs = buffers.compactMap { $0.mData?.assumingMemoryBound(to: Float.self) }
                guard !ptrs.isEmpty else { return }
                for i in 0..<n {
                    var v: Float = 0
                    for p in ptrs { v += p[i] }
                    self.push(v / Float(ptrs.count), ratio: ratio)
                }
            } else {
                guard let p = buffers[0].mData?.assumingMemoryBound(to: Float.self) else { return }
                let n = Int(buffers[0].mDataByteSize) / 4 / channels
                for i in 0..<n {
                    var v: Float = 0
                    for c in 0..<channels { v += p[i * channels + c] }
                    self.push(v / Float(channels), ratio: ratio)
                }
            }
        }
    }

    private func design(rate: Double) {
        let mid = Double(taps - 1) / 2, fc = 7000.0 / rate
        var hh = [Float](repeating: 0, count: taps)
        var sum = 0.0
        for k in 0..<taps {
            let t = Double(k) - mid
            let sinc = t == 0 ? 2 * fc : sin(2 * Double.pi * fc * t) / (Double.pi * t)
            let win = 0.42 - 0.5 * cos(2 * Double.pi * Double(k) / Double(taps - 1)) + 0.08 * cos(4 * Double.pi * Double(k) / Double(taps - 1))
            hh[k] = Float(sinc * win)
            sum += sinc * win
        }
        h = hh.map { $0 / Float(sum) }
        ring = [Float](repeating: 0, count: taps + 1)
        w = 0; m = -1; next = 0; hRate = rate
    }

    private func fir(_ i: Int) -> Float {
        let L = taps + 1
        var j = ((w - 1 - (m - i)) % L + L) % L
        var acc: Float = 0
        for k in 0..<taps {
            acc += h[k] * ring[j]
            j = j == 0 ? L - 1 : j - 1
        }
        return acc
    }

    private func push(_ v: Float, ratio: Double) {
        if abs(v) > peak { peak = abs(v) }
        if ratio <= 1 { emit(v); return }
        let rate = ratio * target
        if rate != hRate { design(rate: rate) }
        ring[w] = v
        w = (w + 1) % (taps + 1)
        m += 1
        while next + 1 <= Double(m) {
            let i0 = Int(next.rounded(.down))
            let frac = Float(next - Double(i0))
            let y0 = fir(i0)
            emit(frac > 0 ? y0 + frac * (fir(i0 + 1) - y0) : y0)
            next += ratio
        }
    }

    private func emit(_ v: Float) {
        env = max(abs(v), env * 0.99996)               // ~1.5 s release at 16 kHz
        let want = env > 0.0005 ? min(12, max(1, 0.5 / env)) : gain
        gain += (want - gain) * (want < gain ? 0.01 : 0.0005) // fast attack, slow recovery
        let s = max(-1, min(1, v * gain))
        out.append(Int16(s < 0 ? s * 32768 : s * 32767))
        if out.count >= frame {
            let written = out.withUnsafeBytes { raw in write(1, raw.baseAddress, raw.count) }
            if written < 0 { exit(0) } // parent went away
            out.removeAll(keepingCapacity: true)
        }
    }
}

let capturer = Capturer()
Task {
    do {
        try await capturer.start()
        log("ready")
    } catch {
        log("error: \(error.localizedDescription)")
        exit(2)
    }
}

// Quit when the parent closes our stdin.
DispatchQueue.global().async {
    while true {
        let d = FileHandle.standardInput.availableData
        if d.isEmpty { exit(0) }
    }
}

dispatchMain()
