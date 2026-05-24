import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import "./index.css";
import { AppLayout } from "@/components/AppLayout";
import { Landing } from "@/pages/Landing";
import { DoctorWorkspace } from "@/pages/DoctorWorkspace";
import { PayerInbox } from "@/pages/PayerInbox";
import { PayerCaseDetail } from "@/pages/PayerCaseDetail";
import { Performance } from "@/pages/Performance";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
      staleTime: 30_000,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<AppLayout />}>
            <Route path="/" element={<Landing />} />
            <Route path="/doctor" element={<DoctorWorkspace />} />
            <Route path="/payer" element={<PayerInbox />} />
            <Route path="/payer/:caseId" element={<PayerCaseDetail />} />
            <Route path="/performance" element={<Performance />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
