import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.util.Base64;

import com.machinezoo.sourceafis.FingerprintImage;
import com.machinezoo.sourceafis.FingerprintMatcher;
import com.machinezoo.sourceafis.FingerprintTemplate;

public class EgisSourceAfisWorker {
    private static final String VERSION = "3.18.1";

    public static void main(String[] args) throws Exception {
        var input = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.US_ASCII));
        var output = new PrintWriter(System.out, true, StandardCharsets.US_ASCII);
        String line;
        while ((line = input.readLine()) != null) {
            try {
                var fields = line.split("\\t", -1);
                switch (fields[0]) {
                    case "PING":
                        output.println("PONG\t" + VERSION);
                        break;
                    case "EXTRACT": {
                        if (fields.length != 5) throw new IllegalArgumentException("bad EXTRACT request");
                        int width = Integer.parseInt(fields[1]);
                        int height = Integer.parseInt(fields[2]);
                        double dpi = Double.parseDouble(fields[3]);
                        byte[] pixels = Base64.getDecoder().decode(fields[4]);
                        if (width <= 0 || height <= 0 || pixels.length != width * height)
                            throw new IllegalArgumentException("invalid grayscale image");
                        var image = new FingerprintImage(width, height, pixels).dpi(dpi);
                        var template = new FingerprintTemplate(image);
                        output.println("TEMPLATE\t" + Base64.getEncoder().encodeToString(template.toByteArray()));
                        break;
                    }
                    case "COMPARE": {
                        if (fields.length != 3) throw new IllegalArgumentException("bad COMPARE request");
                        var probe = new FingerprintTemplate(Base64.getDecoder().decode(fields[1]));
                        var enrolled = new FingerprintTemplate(Base64.getDecoder().decode(fields[2]));
                        double score = new FingerprintMatcher(enrolled).match(probe);
                        output.println("SCORE\t" + Double.toString(score));
                        break;
                    }
                    default:
                        throw new IllegalArgumentException("unknown command");
                }
            } catch (Throwable error) {
                output.println("ERROR\t" + error.getClass().getSimpleName());
            }
        }
    }
}
