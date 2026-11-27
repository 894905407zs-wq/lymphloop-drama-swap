// Scan every decoded frame. OCR only proposes boxes; the agent reviews overlay vs label.
import Foundation
import AVFoundation
import Vision

func fail(_ text: String) -> Never { FileHandle.standardError.write(Data((text+"\n").utf8)); exit(1) }
let args=CommandLine.arguments
// Optional 4th/5th arguments (2026-11-11, any competitor): the fuzzy pattern on the upper-cased letters and the target word.
// Optional 6th/7th (2026-11-19, speed): STEP (OCR every STEP-th frame) and RANGES ("a-b,c-d" frames, end exclusive;
// empty = all). Several processes scan disjoint ranges at once; frame numbers come from the presentation time.
if args.count != 4 && args.count != 6 && args.count != 8 { fail("Usage: scan_watermark SOURCE OUTPUT_JSON SOURCE_SHA256 [PATTERN TARGET [STEP RANGES]]") }
let step = args.count == 8 ? max(1, Int(args[6]) ?? 1) : 1
var ranges: [(Int, Int)] = []
if args.count == 8 && !args[7].isEmpty {
    for part in args[7].split(separator: ",") {
        let ab = part.split(separator: "-"); if ab.count == 2, let a = Int(ab[0]), let b = Int(ab[1]) { ranges.append((a, b)) }
    }
}
let pattern = args.count == 6 ? args[4] : "(PHOR|HORI|RHOR|MPHO(?!O))"
let target = args.count == 6 ? args[5] : "LYMPHORIA"
let url=URL(fileURLWithPath:args[1]); let asset=AVURLAsset(url:url)
guard let track=asset.tracks(withMediaType:.video).first else { fail("No video track") }
let transform=track.preferredTransform
if abs(transform.b)>0.001 || abs(transform.c)>0.001 || transform.a<0 || transform.d<0 { fail("Normalize display rotation before watermark scanning") }
let reader:AVAssetReader
do { reader=try AVAssetReader(asset:asset) } catch { fail("Cannot open asset") }
let output=AVAssetReaderTrackOutput(track:track,outputSettings:[kCVPixelBufferPixelFormatTypeKey as String:kCVPixelFormatType_32BGRA]);output.alwaysCopiesSampleData=false
reader.add(output)
let fps = Double(track.nominalFrameRate) > 0 ? Double(track.nominalFrameRate) : 30.0
if !ranges.isEmpty {
    let lo = ranges.map { $0.0 }.min()!, hi = ranges.map { $0.1 }.max()!
    let start = CMTime(seconds: max(0, Double(lo) - 1) / fps, preferredTimescale: 600000)
    reader.timeRange = CMTimeRange(start: start, duration: CMTime(seconds: Double(hi - lo + 2) / fps, preferredTimescale: 600000))
}
if !reader.startReading() { fail("Cannot decode input") }
var cpuFallbackFrames=0
var detections=[[String:Any]](); var frame=0; var scanned=0; var decoded=0
func wanted(_ f: Int) -> Bool {
    if !ranges.isEmpty && !ranges.contains(where: { f >= $0.0 && f < $0.1 }) { return false }
    let base = ranges.first(where: { f >= $0.0 && f < $0.1 })?.0 ?? 0
    return (f - base) % step == 0
}
while let sample=output.copyNextSampleBuffer() {
    guard let pixel=CMSampleBufferGetImageBuffer(sample) else { fail("Missing decoded pixel buffer") }
    frame = Int((CMSampleBufferGetPresentationTimeStamp(sample).seconds * fps).rounded()); decoded+=1
    if !wanted(frame) { continue }
    scanned+=1
    let width=CVPixelBufferGetWidth(pixel),height=CVPixelBufferGetHeight(pixel)
    autoreleasepool {
        let request=VNRecognizeTextRequest();request.recognitionLevel = .accurate;request.recognitionLanguages=["en-US"];request.usesLanguageCorrection=false;request.minimumTextHeight=0.005
        do { try VNImageRequestHandler(cvPixelBuffer:pixel,options:[:]).perform([request]) } catch {
            // Under concurrent Vision load the default compute device can refuse
            // a frame. Retry that SAME frame on CPU; never skip OCR or coverage.
            request.usesCPUOnly=true
            do { try VNImageRequestHandler(cvPixelBuffer:pixel,options:[:]).perform([request]); cpuFallbackFrames+=1 }
            catch { fail("OCR failed on default device and CPU: \(error)") }
        }
        for result in request.results ?? [] {
            guard let candidate=result.topCandidates(1).first else { continue }
            // Faint watermarks rarely read cleanly (LYMPHORTA, LYMRHORIA, MPHORIA, @LYMPHOIN ... 2026-10-25: the old exact
            // "LYMPHORIA" match missed every one of them). Keep any PHOR/HORI/RHOR/MPHO fragment; the cursive LymphLoop logo
            // reads as "LyMPHOR" (lowercase or Cyrillic y after L) and the stamped LYMPHLOOP never has PHOR/HORI.
            let letters=candidate.string.uppercased().filter { $0.isLetter }
            if letters.range(of:pattern, options:.regularExpression)==nil { continue }
            if candidate.string.range(of:"L[yу]", options:.regularExpression) != nil { continue }
            // Whole line box: the droplet icon in front of the word is part of the watermark (user, 2026-10-25).
            let box=result.boundingBox
            let x=max(0,Int(floor(box.minX*Double(width)))-2), y=max(0,Int(floor((1-box.maxY)*Double(height)))-2)
            let right=min(width,Int(ceil(box.maxX*Double(width)))+2), bottom=min(height,Int(ceil((1-box.minY)*Double(height)))+2)
            detections.append(["frame":frame,"text":candidate.string,"confidence":candidate.confidence,"bbox":[x,y,right-x,bottom-y],"classification":"unreviewed"])
        }
    }
}
if reader.status != .completed { fail("Full decode failed: \(String(describing: reader.error))") }
let record:[String:Any] = ["cpu_fallback_frames":cpuFallbackFrames,"type":"lymphloop_watermark_scan","target":target,"source":["path":url.standardizedFileURL.path,"sha256":args[3]],"frame_count":frame+1,"scanned_frames":scanned,"decoded_frames":decoded,"step":step,"coverage":(step == 1 && ranges.isEmpty ? "every_frame" : "partial"),"detections":detections,"overlay_classification_reviewed":false,"engine":"macOS Vision accurate OCR, fuzzy \(target) match \(pattern), whole-line boxes; detections are candidates, not proof of semantic absence"]
do { let data=try JSONSerialization.data(withJSONObject:record,options:[.prettyPrinted,.sortedKeys]);try data.write(to:URL(fileURLWithPath:args[2]),options:.atomic) } catch { fail("Cannot write scan") }
