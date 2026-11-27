// Faces and people in still images (macOS Vision). Usage: scan_people IMG...  -> JSON lines {"path","faces":[[x,y,w,h]],"people":[[x,y,w,h]]}
// Boxes are normalised, origin top-left.
import Foundation
import Vision
import AppKit

func boxes(_ obs: [VNDetectedObjectObservation]?, minConf: Float) -> [[Double]] {
    (obs ?? []).filter { $0.confidence >= minConf }.map { o in
        let b = o.boundingBox
        return [Double(b.minX), Double(1 - b.maxY), Double(b.width), Double(b.height)]
    }
}
for path in CommandLine.arguments.dropFirst() {
    guard let img = NSImage(contentsOfFile: path), let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        print("{\"path\":\"\(path)\",\"error\":\"unreadable\"}"); continue
    }
    let faces = VNDetectFaceRectanglesRequest()
    let people = VNDetectHumanRectanglesRequest(); people.upperBodyOnly = false
    let h = VNImageRequestHandler(cgImage: cg, options: [:])
    try? h.perform([faces, people])
    let out: [String: Any] = ["path": path, "faces": boxes(faces.results, minConf: 0.5), "people": boxes(people.results, minConf: 0.5)]
    let d = try! JSONSerialization.data(withJSONObject: out)
    print(String(data: d, encoding: .utf8)!)
}
