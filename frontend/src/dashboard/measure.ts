// Text widths for laying out a chart, measured with the page's own font on a canvas that is never shown. Where no
// canvas is available the width is estimated from the character count.

let context: CanvasRenderingContext2D | null | undefined;
let family = "";

export function rootPx(): number {
  return parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
}

export function textWidth(text: string, px: number, weight = 400): number {
  if (context === undefined) {
    context = document.createElement("canvas").getContext("2d");
    family = getComputedStyle(document.body).fontFamily;
  }
  if (!context) return text.length * px * 0.56;
  context.font = `${weight} ${px}px ${family}`;
  return context.measureText(text).width;
}

/** The text, shortened with an ellipsis until it fits the width. The whole of it stays in the chart's table. */
export function fitText(text: string, px: number, width: number, weight = 400): string {
  if (textWidth(text, px, weight) <= width) return text;
  let low = 0;
  let high = text.length;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (textWidth(`${text.slice(0, middle).trimEnd()}…`, px, weight) <= width) low = middle;
    else high = middle - 1;
  }
  return `${text.slice(0, Math.max(low, 1)).trimEnd()}…`;
}
