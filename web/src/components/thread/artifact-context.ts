import { createContext } from "react";

type Setter<T> = (value: T | ((value: T) => T)) => void;

export const ArtifactSlotContext = createContext<{
  open: [string | null, Setter<string | null>];
  mounted: [string | null, Setter<string | null>];

  title: [HTMLElement | null, Setter<HTMLElement | null>];
  content: [HTMLElement | null, Setter<HTMLElement | null>];

  context: [Record<string, unknown>, Setter<Record<string, unknown>>];
}>(null!);
