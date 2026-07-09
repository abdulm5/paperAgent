import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import App from "./App";

test("renders the PagerAgent project shell", () => {
  render(<App />);

  expect(screen.getByRole("heading", { name: "PagerAgent" })).toBeInTheDocument();
  expect(screen.getByText(/Foundation complete/i)).toBeInTheDocument();
});
