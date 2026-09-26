import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom has no canvas. Charts measure text on one, and fall back to an estimate without it.
HTMLCanvasElement.prototype.getContext = () => null;
// Nor does it scroll. A new question's turn scrolls into view.
Element.prototype.scrollIntoView = () => undefined;

afterEach(() => {
  cleanup();
});
