import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ChatPage } from "@/chat/ChatPage";
import type { SessionView } from "@/lib/types";
import { dana, figures, streamOf } from "@/test/fixtures";
import { fakeServer, json, renderWithQueries } from "@/test/server";

const demo: SessionView = {
  me: dana,
  demo: true,
  users: [
    { user_id: "dana", name: "Dana Reyes", title: "Claims adjuster, West" },
    { user_id: "priya", name: "Priya Natarajan", title: "Claims supervisor, all regions" },
  ],
};

describe("the chat page", () => {
  it("asks as the signed-in user and streams the answer, its evidence and its footer", async () => {
    const calls = fakeServer({
      "GET /api/session": () => json(demo),
      "POST /ask": () => new Response(streamOf(figures), { headers: { "Content-Type": "text/event-stream" } }),
    });
    const { container } = renderWithQueries(<ChatPage />);
    expect(await screen.findByRole("heading", { name: "Ask about claims, payments and policy wording" })).toBeVisible();
    expect(container.querySelector("#intro")).toHaveTextContent(
      "Questions run as Dana Reyes on the database login u_adj_west.",
    );
    const picker = screen.getByRole("combobox", { name: "Asking as" });
    expect(picker).toHaveValue("dana");
    expect(screen.getByText("Demo identity, stands in for SSO")).toBeVisible();
    expect(screen.queryByRole("link", { name: "Dashboard" })).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /How much did we pay on hail claims/ }));
    const box = screen.getByRole("textbox", { name: "Your question" });
    expect(box).toHaveValue("How much did we pay on hail claims in Colorado in Q2 2025?");
    expect(box).toHaveFocus();
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    const footer = await waitFor(() => {
      const found = container.querySelector(".turn .footer");
      if (!found) throw new Error("no footer yet");
      return found;
    });
    expect(calls.find((call) => call.url === "/ask")?.body).toEqual({
      q: "How much did we pay on hail claims in Colorado in Q2 2025?",
    });
    expect(container.querySelector("#intro")).toBeNull();
    expect(container.querySelector(".turn .question")?.textContent).toBe(
      "How much did we pay on hail claims in Colorado in Q2 2025?",
    );
    expect(container.querySelector(".turn .answer-text")).toHaveTextContent("We paid $4,108,453");
    expect(container.querySelector(".turn details.evidence")).toBeInTheDocument();
    expect(footer).toHaveTextContent("Finished in 62 ms");
    expect(footer).toHaveTextContent("Request 5b0c1f3e");
    expect(footer).toHaveTextContent("Route quantitative");
    expect(box).toHaveValue("");
  });

  it("shows the server's sentence when a question is turned away", async () => {
    const said = "You have asked 20 questions in the last minute. Wait 3 seconds and try again.";
    fakeServer({
      "GET /api/session": () => json(demo),
      "POST /ask": () => new Response(said, { status: 429 }),
    });
    const { container } = renderWithQueries(<ChatPage />);
    const box = await screen.findByRole("textbox", { name: "Your question" });
    await userEvent.type(box, "How many claims?{Enter}");
    await waitFor(() => {
      expect(container.querySelector(".notice.error")).toBeInTheDocument();
    });
    expect(container.querySelector(".notice.error .label")?.textContent).toBe("Not sent");
    expect(container.querySelector(".notice.error p")?.textContent).toBe(said);
    expect(container.querySelector(".footer")).toBeNull();
  });

  it("names the user behind the sign-in proxy, with no picker", async () => {
    fakeServer({ "GET /api/session": () => json({ ...demo, demo: false, users: [], me: { ...dana, ops: true } }) });
    renderWithQueries(<ChatPage />);
    expect(await screen.findByText("Dana Reyes, Claims adjuster, West")).toBeVisible();
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.queryByText("Demo identity, stands in for SSO")).toBeNull();
    expect(screen.getByRole("link", { name: "Dashboard" })).toHaveAttribute("href", "/dashboard");
  });
});
