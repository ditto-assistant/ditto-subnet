import { QueryClientProvider } from "@tanstack/solid-query";
import { render } from "solid-js/web";

import App from "./App";
import { queryClient } from "./data/queryClient";
import "./styles/index.css";

const root = document.getElementById("root");

if (!root) throw new Error("Dashboard root element is missing");

render(
  () => (
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  ),
  root,
);
