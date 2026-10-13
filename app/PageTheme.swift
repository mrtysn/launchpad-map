import AppKit
import MacAppStyle

/// The history page is HTML with its own dark colours. The window sets the style's colours over them,
/// resolved for the current appearance, so the page follows the system light and dark like the chrome.
@MainActor
enum PageTheme {
    private static func rgb(_ color: NSColor, in appearance: NSAppearance, alpha: CGFloat = 1) -> String {
        var out = "rgba(0,0,0,1)"
        appearance.performAsCurrentDrawingAppearance {
            let c = color.usingColorSpace(.sRGB) ?? color
            out = "rgba(\(Int((c.redComponent * 255).rounded())),\(Int((c.greenComponent * 255).rounded())),"
                + "\(Int((c.blueComponent * 255).rounded())),\(alpha))"
        }
        return out
    }

    static func css(for appearance: NSAppearance) -> String {
        let dark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        func c(_ color: NSColor, _ alpha: CGFloat = 1) -> String { rgb(color, in: appearance, alpha: alpha) }
        let bg = c(Style.Palette.background), surface = c(Style.Palette.surface), text = c(Style.Palette.text)
        let dim = c(Style.Palette.dim), frame = c(Style.Palette.frame)
        let wash = c(Style.Palette.text, 0.06), selected = c(Style.Palette.text, 0.10)
        return """
        html:root { color-scheme: \(dark ? "dark" : "light"); --ink: \(text); --dim: \(dim); --faint: \(dim);
          --line: \(frame); --glass: \(wash);
          --moved: \(c(Style.Palette.warn)); --new: \(c(Style.Palette.success)); --gone: \(c(Style.Palette.danger));
          --warn: \(c(Style.Palette.danger)); }
        .usage.usage { --bar: \(c(Style.Palette.info)); --bar2: \(c(Style.Palette.warn)); --never: \(c(Style.Palette.danger));
          --rare: \(c(Style.Palette.warn)); --often: \(c(Style.Palette.success)); }
        body { background: \(bg) !important; font-size: 12px !important; }
        .rail { background: \(surface) !important; }
        .snap:hover { background: \(wash) !important; }
        .snap[aria-current="true"] { background: \(selected) !important; }
        .dot { background: \(bg) !important; }
        .snap[aria-current="true"] .dot { background: \(text) !important; box-shadow: 0 0 0 5px \(c(Style.Palette.text, 0.14)) !important; }
        .find input, .compare select { background: \(surface) !important; border-radius: \(Style.Metric.radius)px !important; }
        .trail, .sheet { background: \(surface) !important; backdrop-filter: none !important; }
        .chip[aria-pressed="true"] { background: \(selected) !important; }
        .toggle[aria-pressed="true"], .devices button[aria-pressed="true"] { background: \(text) !important; color: \(bg) !important; }
        .page { background: \(surface) !important; }
        .screen { background: \(bg) !important; }
        .screen .dockbar, .widget { background: \(wash) !important; }
        .screen .count { background: \(bg) !important; }
        .mark { border-color: \(surface) !important; }
        .ph { background: \(selected) !important; }
        .folder { background: \(c(Style.Palette.text, 0.12)) !important; }
        .label { text-shadow: none !important; }
        .storemenu { background: \(surface) !important; color: \(text) !important; border: 1px solid \(frame); box-shadow: none !important; }
        .storemenu a:hover { background: \(selected) !important; }
        dialog::backdrop { background: \(c(Style.Palette.background, 0.7)) !important; backdrop-filter: none !important; }
        """
    }

    /// A script that sets the style on the page; run again when the appearance changes.
    static func update(for appearance: NSAppearance) -> String {
        let css = css(for: appearance).replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "`", with: "\\`")
        return """
        (function () {
          var s = document.getElementById('home-screens-theme');
          if (!s) { s = document.createElement('style'); s.id = 'home-screens-theme'; document.documentElement.appendChild(s); }
          s.textContent = `\(css)`;
        })();
        """
    }

    static func script(for appearance: NSAppearance) -> String { update(for: appearance) }
}
