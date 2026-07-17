import { ContentBlock } from "@langchain/core/messages";
import { toast } from "sonner";

export const supportedImageTypes = [
  "image/jpeg",
  "image/png",
  "image/gif",
  "image/webp",
];

export const supportedDocumentTypes = [
  "application/pdf",
  "text/plain",
  "text/markdown",
  "text/csv",
  "application/msword",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.ms-excel",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
];

export const supportedFileTypes = [
  ...supportedImageTypes,
  ...supportedDocumentTypes,
];

export const supportedExtensionMimeTypes: Record<string, string> = {
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".png": "image/png",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".pdf": "application/pdf",
  ".txt": "text/plain",
  ".md": "text/markdown",
  ".markdown": "text/markdown",
  ".csv": "text/csv",
  ".doc": "application/msword",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".xls": "application/vnd.ms-excel",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
};

export const supportedUploadDescription =
  "JPEG, PNG, GIF, WEBP image, PDF, TXT, Word, CSV, or Excel file";

export function getSupportedUploadMimeType(file: File): string | null {
  if (supportedFileTypes.includes(file.type)) {
    return file.type;
  }

  const extension = file.name
    .slice(file.name.lastIndexOf("."))
    .toLowerCase();
  return supportedExtensionMimeTypes[extension] ?? null;
}

// Returns a Promise of a typed multimodal block for images or stored file references.
export async function fileToContentBlock(
  file: File,
): Promise<ContentBlock.Multimodal.Data> {
  const mimeType = getSupportedUploadMimeType(file);
  if (!mimeType) {
    toast.error(
      `Unsupported file type: ${file.type}. Supported types are: ${supportedFileTypes.join(", ")}`,
    );
    return Promise.reject(new Error(`Unsupported file type: ${file.type}`));
  }

  if (supportedImageTypes.includes(mimeType)) {
    const data = await fileToBase64(file);
    return {
      type: "image",
      mimeType,
      data,
      metadata: { name: file.name },
    };
  }

  const uploaded = await uploadFileReference(file);
  return {
    type: "file",
    mimeType: uploaded.mimeType,
    data: "",
    metadata: {
      filename: uploaded.filename,
      uploadId: uploaded.uploadId,
      path: uploaded.path,
      size: uploaded.size,
      stored: true,
    },
  };
}

export interface UploadedFileReference {
  uploadId: string;
  filename: string;
  mimeType: string;
  size: number;
  path: string;
}

async function uploadFileReference(file: File): Promise<UploadedFileReference> {
  const formData = new FormData();
  formData.append("file", file);
  const response = await fetch("/api/local/uploads", {
    method: "POST",
    body: formData,
  });
  const body = await response.json();
  if (!response.ok) {
    throw new Error(body?.error || "Failed to upload file.");
  }
  return body;
}

export function uploadedFileBlockToText(
  block: ContentBlock.Multimodal.Data,
): { type: "text"; text: string } | null {
  if (block.type !== "file" || !block.metadata?.stored) {
    return null;
  }

  const filename = String(block.metadata.filename || block.metadata.name || "uploaded file");
  const mimeType = String(block.mimeType || "application/octet-stream");
  const uploadId = String(block.metadata.uploadId || "");
  const filePath = String(block.metadata.path || "");
  const size = Number(block.metadata.size || 0);

  return {
    type: "text",
    text: [
      "[Uploaded file reference]",
      `filename: ${filename}`,
      `mimeType: ${mimeType}`,
      `uploadId: ${uploadId}`,
      `size: ${size} bytes`,
      `path: ${filePath}`,
      "The file content is stored locally and is not embedded in this message. Use file/PDF tools or shell utilities to inspect it when needed.",
    ].join("\n"),
  };
}

// Helper to convert File to base64 string
export async function fileToBase64(file: File): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => {
      const result = reader.result as string;
      // Remove the data:...;base64, prefix
      resolve(result.split(",")[1]);
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

// Type guard for Base64ContentBlock
export function isBase64ContentBlock(
  block: unknown,
): block is ContentBlock.Multimodal.Data {
  if (typeof block !== "object" || block === null || !("type" in block))
    return false;
  // file type (legacy)
  if (
      (block as { type: unknown }).type === "file" &&
    "mimeType" in block &&
    typeof (block as { mimeType?: unknown }).mimeType === "string" &&
    (supportedFileTypes.includes((block as { mimeType: string }).mimeType) ||
      (block as { mimeType: string }).mimeType.startsWith("image/"))
  ) {
    return true;
  }
  // image type (new)
  if (
    (block as { type: unknown }).type === "image" &&
    "mimeType" in block &&
    typeof (block as { mimeType?: unknown }).mimeType === "string" &&
    (block as { mimeType: string }).mimeType.startsWith("image/")
  ) {
    return true;
  }
  return false;
}
