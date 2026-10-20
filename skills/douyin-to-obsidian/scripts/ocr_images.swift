import Foundation
import Vision
import ImageIO

// Ordered local files only. No network requests or user data logging.
let paths = Array(CommandLine.arguments.dropFirst())
guard !paths.isEmpty else { fatalError("Provide ordered image paths") }
var pages: [[String: Any]] = []
do {
    for (index, path) in paths.enumerated() {
        let url = URL(fileURLWithPath: path)
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
            throw NSError(domain: "DouyinOCR", code: 1, userInfo: [NSLocalizedDescriptionKey: "Cannot decode page \(index + 1)"])
        }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = ["zh-Hans", "en-US"]
        request.usesLanguageCorrection = true
        try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
        let observations = (request.results ?? []).sorted {
            if abs($0.boundingBox.midY - $1.boundingBox.midY) > 0.015 {
                return $0.boundingBox.midY > $1.boundingBox.midY
            }
            return $0.boundingBox.minX < $1.boundingBox.minX
        }
        let lines: [[String: Any]] = observations.compactMap { observation in
            guard let candidate = observation.topCandidates(1).first else { return nil }
            let box = observation.boundingBox
            return ["text": candidate.string, "confidence": candidate.confidence,
                    "box": [box.minX, box.minY, box.width, box.height]]
        }
        pages.append(["page": index + 1, "file": path, "width": image.width, "height": image.height,
                      "text": lines.compactMap { $0["text"] as? String }.joined(separator: "\n"), "lines": lines])
    }
    let data = try JSONSerialization.data(withJSONObject: ["engine": "Apple Vision", "pages": pages], options: [.prettyPrinted, .sortedKeys])
    FileHandle.standardOutput.write(data)
} catch {
    FileHandle.standardError.write(Data("OCR failed: \(error.localizedDescription)\n".utf8))
    exit(1)
}
