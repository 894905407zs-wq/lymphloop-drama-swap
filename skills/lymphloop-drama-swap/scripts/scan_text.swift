// Scan every decoded frame of a (short) clip and record every recognised text box.
// Used by caption_clean.py to find burned-in captions in a reference clip before it is sent to generation.
import Foundation
import AVFoundation
import Vision

func fail(_ text: String) -> Never { FileHandle.standardError.write(Data((text+"\n").utf8)); exit(1) }
let args=CommandLine.arguments
if args.count != 3 { fail("Usage: scan_text SOURCE OUTPUT_JSON") }
let url=URL(fileURLWithPath:args[1]); let asset=AVURLAsset(url:url)
guard let track=asset.tracks(withMediaType:.video).first else { fail("No video track") }
let reader:AVAssetReader
do { reader=try AVAssetReader(asset:asset) } catch { fail("Cannot open asset") }
let output=AVAssetReaderTrackOutput(track:track,outputSettings:[kCVPixelBufferPixelFormatTypeKey as String:kCVPixelFormatType_32BGRA]);output.alwaysCopiesSampleData=false
reader.add(output)
if !reader.startReading() { fail("Cannot decode input") }
var cpuFallbackFrames=0
var detections=[[String:Any]](); var frame=0; var width=0; var height=0
while let sample=output.copyNextSampleBuffer() {
    guard let pixel=CMSampleBufferGetImageBuffer(sample) else { fail("Missing decoded pixel buffer") }
    width=CVPixelBufferGetWidth(pixel); height=CVPixelBufferGetHeight(pixel)
    autoreleasepool {
        let request=VNRecognizeTextRequest();request.recognitionLevel = .accurate;request.recognitionLanguages=["en-US"];request.usesLanguageCorrection=false;request.minimumTextHeight=0.01
        do { try VNImageRequestHandler(cvPixelBuffer:pixel,options:[:]).perform([request]) } catch {
            // Under concurrent Vision load the default compute device can refuse
            // a frame. Retry that SAME frame on CPU; never skip OCR or coverage.
            request.usesCPUOnly=true
            do { try VNImageRequestHandler(cvPixelBuffer:pixel,options:[:]).perform([request]); cpuFallbackFrames+=1 }
            catch { fail("OCR failed on default device and CPU: \(error)") }
        }
        for result in request.results ?? [] {
            guard let candidate=result.topCandidates(1).first else { continue }
            let box=result.boundingBox
            let x=max(0,Int(floor(box.minX*Double(width)))), y=max(0,Int(floor((1-box.maxY)*Double(height))))
            let right=min(width,Int(ceil(box.maxX*Double(width)))), bottom=min(height,Int(ceil((1-box.minY)*Double(height))))
            detections.append(["frame":frame,"text":candidate.string,"confidence":candidate.confidence,"bbox":[x,y,right-x,bottom-y]])
        }
    }
    frame+=1
}
if reader.status != .completed { fail("Full decode failed: \(String(describing: reader.error))") }
let record:[String:Any] = ["cpu_fallback_frames":cpuFallbackFrames,"type":"lymphloop_text_scan","source":url.standardizedFileURL.path,"frame_count":frame,"width":width,"height":height,"detections":detections,"engine":"macOS Vision accurate OCR, every frame"]
do { let data=try JSONSerialization.data(withJSONObject:record,options:[.sortedKeys]);try data.write(to:URL(fileURLWithPath:args[2]),options:.atomic) } catch { fail("Cannot write scan") }
