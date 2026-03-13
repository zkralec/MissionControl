import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReactQueryDevtools } from "@tanstack/react-query-devtools";
import { setRuntimeApiKey } from "@/lib/api/client";

type ApiRuntimeContextValue = {
  apiKey: string;
  setApiKey: (value: string) => void;
};

const ApiRuntimeContext = createContext<ApiRuntimeContextValue | null>(null);

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 5_000
    }
  }
});

export function AppProviders({ children }: { children: ReactNode }): JSX.Element {
  const [apiKey, setApiKey] = useState("");

  useEffect(() => {
    setRuntimeApiKey(apiKey);
  }, [apiKey]);

  const contextValue = useMemo(() => ({ apiKey, setApiKey }), [apiKey]);
  const enableDevtools = String(import.meta.env.VITE_ENABLE_QUERY_DEVTOOLS || "false") === "true";

  return (
    <ApiRuntimeContext.Provider value={contextValue}>
      <QueryClientProvider client={queryClient}>
        {children}
        {enableDevtools ? <ReactQueryDevtools initialIsOpen={false} /> : null}
      </QueryClientProvider>
    </ApiRuntimeContext.Provider>
  );
}

export function useApiRuntime(): ApiRuntimeContextValue {
  const ctx = useContext(ApiRuntimeContext);
  if (!ctx) {
    throw new Error("useApiRuntime must be used within AppProviders");
  }
  return ctx;
}
