import { ReactNode, useContext, useEffect } from "react";
import { createPortal } from "react-dom";
import { ArtifactSlotContext } from "./artifact-context";

export function ArtifactSlot(props: {
  id: string;
  children?: ReactNode;
  title?: ReactNode;
}) {
  const context = useContext(ArtifactSlotContext);

  const [ctxMounted, ctxSetMounted] = context.mounted;
  const [content] = context.content;
  const [title] = context.title;

  const isMounted = ctxMounted === props.id;
  const isEmpty = props.children == null && props.title == null;

  useEffect(() => {
    if (isEmpty) {
      ctxSetMounted((open) => (open === props.id ? null : open));
    }
  }, [isEmpty, ctxSetMounted, props.id]);

  if (!isMounted) return null;
  return (
    <>
      {title != null ? createPortal(<>{props.title}</>, title) : null}
      {content != null ? createPortal(<>{props.children}</>, content) : null}
    </>
  );
}
