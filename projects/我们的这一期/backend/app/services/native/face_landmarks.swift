// Local geometry only. No identity recognition, network or media persistence.
import Foundation
import Vision
import ImageIO

let folder = URL(fileURLWithPath: CommandLine.arguments[1])
let files = try FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil)
    .filter { $0.pathExtension == "jpg" }.sorted { $0.lastPathComponent < $1.lastPathComponent }
var output: [[String: Any]] = []
for file in files {
    try autoreleasepool {
        let request = VNDetectFaceLandmarksRequest()
        let handler = VNImageRequestHandler(url: file, options: [:])
        try handler.perform([request])
        let faces: [[String: Any]] = (request.results ?? []).map { face in
            let b = face.boundingBox
            func points(_ region: VNFaceLandmarkRegion2D?) -> [[Double]] {
                guard let region = region else { return [] }
                return region.normalizedPoints.map { p in
                    [Double(b.minX + CGFloat(p.x) * b.width), Double(1 - b.minY - CGFloat(p.y) * b.height)]
                }
            }
            return ["confidence": face.confidence,
                "box": [b.minX, 1-b.maxY, b.width, b.height],
                "eyes": [points(face.landmarks?.leftEye), points(face.landmarks?.rightEye)],
                "mouth": points(face.landmarks?.outerLips)]
        }
        output.append(["frame": file.deletingPathExtension().lastPathComponent, "faces": faces])
    }
}
let data = try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys])
FileHandle.standardOutput.write(data)
