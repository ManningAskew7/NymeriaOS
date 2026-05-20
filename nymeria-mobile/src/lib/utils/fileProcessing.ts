/**
 * File processing utilities for handling file uploads.
 * Validates, resizes images, and converts files to base64 for multimodal messages.
 */

import type { AttachmentLimits, FileAttachment, FileType } from '$lib/types';

const MIME_FALLBACK_BY_EXTENSION: Record<string, string> = {
  '.md': 'text/markdown',
  '.markdown': 'text/markdown',
  '.txt': 'text/plain',
  '.csv': 'text/csv',
  '.pdf': 'application/pdf',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.gif': 'image/gif',
  '.webp': 'image/webp'
};

// File constraints by type. These are *absolute* client-side ceilings,
// independent of the per-model caps the backend reports via
// /threads/:id/attachment_limits — both apply. Whichever bites first wins.
// (The per-model cap is typically the image *count*; MAX_SIZE is per-file.)
export const FILE_CONSTRAINTS = {
  image: {
    MAX_SIZE: 10 * 1024 * 1024,           // 10MB max file size
    RESIZE_THRESHOLD: 4 * 1024 * 1024,    // 4MB threshold for auto-resize
    MAX_DIMENSION: 2048,                   // Max width/height after resize
    COMPRESSION_QUALITY: 0.85,             // JPEG compression quality
    TYPES: ['image/jpeg', 'image/png', 'image/gif', 'image/webp'] as const
  },
  document: {
    MAX_SIZE: 20 * 1024 * 1024,           // 20MB max file size
    TYPES: ['application/pdf', 'text/plain', 'text/markdown', 'text/csv'] as const
  }
};

// Conservative defaults applied before the backend cap fetch lands (or when
// it fails). Mirrors `_DEFAULT_ATTACHMENT_LIMITS` in
// nymeria/config/model_capabilities.py — keep in sync.
export const DEFAULT_ATTACHMENT_LIMITS: AttachmentLimits = {
  max_images_per_request: 16,
  max_image_bytes: 5 * 1024 * 1024,
  max_pdf_pages: 100,
  max_total_bytes: 32 * 1024 * 1024
};

export type SupportedImageType = typeof FILE_CONSTRAINTS.image.TYPES[number];
export type SupportedDocumentType = typeof FILE_CONSTRAINTS.document.TYPES[number];

export interface FileProcessingError {
  type: 'invalid_type' | 'too_large' | 'processing_failed';
  message: string;
}

function getLowercaseExtension(fileName: string): string {
  const dotIndex = fileName.lastIndexOf('.');
  if (dotIndex < 0) return '';
  return fileName.slice(dotIndex).toLowerCase();
}

/**
 * Infer MIME type from browser-provided MIME and filename fallback.
 */
export function inferMimeType(mimeType: string, fileName: string): string {
  const normalized = mimeType.trim().toLowerCase();
  if (normalized && normalized !== 'application/octet-stream' && normalized !== 'binary/octet-stream') {
    return normalized;
  }

  const ext = getLowercaseExtension(fileName);
  return MIME_FALLBACK_BY_EXTENSION[ext] || normalized;
}


/**
 * Determine the file type category from a MIME type.
 */
export function getFileType(mimeType: string, fileName: string = ''): FileType | null {
  const inferredMimeType = inferMimeType(mimeType, fileName);

  if ((FILE_CONSTRAINTS.image.TYPES as readonly string[]).includes(inferredMimeType)) {
    return 'image';
  }
  if ((FILE_CONSTRAINTS.document.TYPES as readonly string[]).includes(inferredMimeType)) {
    return 'document';
  }
  return null;
}

/**
 * Check if a file is a supported type.
 */
export function isFileSupported(file: File): boolean {
  return getFileType(file.type, file.name) !== null;
}

/**
 * Validate that a file is a supported image type.
 */
export function validateImageType(file: File): boolean {
  return getFileType(file.type, file.name) === 'image';
}

/**
 * Get a user-friendly error message for invalid file type.
 */
export function getTypeErrorMessage(): string {
  return 'Supported: images (JPEG, PNG, GIF, WebP) and documents (PDF, TXT, MD, CSV)';
}

/**
 * Get a user-friendly error message for file too large.
 */
export function getSizeErrorMessage(fileType: FileType): string {
  const maxSize = FILE_CONSTRAINTS[fileType].MAX_SIZE / (1024 * 1024);
  const typeLabel = fileType === 'image' ? 'Image' : 'Document';
  return `${typeLabel} must be under ${maxSize}MB`;
}

/**
 * Get a user-friendly error message for too many images.
 * The cap is per-model (e.g. 100 for claude-opus-4-7, 1500 for gpt-5.5).
 */
export function getMaxImagesErrorMessage(limit: number): string {
  return `Current model accepts at most ${limit} image${limit === 1 ? '' : 's'} per message`;
}


/**
 * Read a file as a data URL (base64).
 */
export function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(new Error('Failed to read file'));
    reader.readAsDataURL(file);
  });
}

/**
 * Load an image from a data URL.
 */
function loadImage(dataUrl: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('Failed to load image'));
    img.src = dataUrl;
  });
}

/**
 * Resize an image to fit within max dimensions while maintaining aspect ratio.
 * Returns the resized image as a JPEG data URL.
 */
export async function resizeImage(
  dataUrl: string,
  maxDimension: number = FILE_CONSTRAINTS.image.MAX_DIMENSION
): Promise<string> {
  const img = await loadImage(dataUrl);

  let { width, height } = img;

  // Calculate new dimensions maintaining aspect ratio
  if (width > maxDimension || height > maxDimension) {
    if (width > height) {
      height = Math.round(height * (maxDimension / width));
      width = maxDimension;
    } else {
      width = Math.round(width * (maxDimension / height));
      height = maxDimension;
    }
  }

  // Create canvas and draw resized image
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;

  const ctx = canvas.getContext('2d');
  if (!ctx) {
    throw new Error('Failed to get canvas context');
  }

  ctx.drawImage(img, 0, 0, width, height);

  // Convert to JPEG with compression
  return canvas.toDataURL('image/jpeg', FILE_CONSTRAINTS.image.COMPRESSION_QUALITY);
}

/**
 * Generate a unique ID for a file attachment.
 */
function generateFileId(): string {
  return `file-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;
}

/**
 * Process an image file into a FileAttachment.
 * Validates the file, resizes if necessary, and converts to base64.
 */
async function processImageFile(file: File): Promise<FileAttachment> {
  // Validate file size
  if (file.size > FILE_CONSTRAINTS.image.MAX_SIZE) {
    throw {
      type: 'too_large',
      message: getSizeErrorMessage('image')
    } as FileProcessingError;
  }

  try {
    let dataUrl = await readFileAsDataUrl(file);
    let mimeType = inferMimeType(file.type, file.name);

    // Some browsers omit MIME in Data URL for unknown file types.
    if (dataUrl.startsWith('data:;base64,') && mimeType) {
      dataUrl = dataUrl.replace('data:;base64,', `data:${mimeType};base64,`);
    }

    // Resize if over threshold
    if (file.size > FILE_CONSTRAINTS.image.RESIZE_THRESHOLD) {
      dataUrl = await resizeImage(dataUrl);
      mimeType = 'image/jpeg'; // Resized images are converted to JPEG
    }

    return {
      id: generateFileId(),
      type: 'image',
      dataUrl,
      mimeType,
      name: file.name,
      size: file.size
    };
  } catch (error) {
    if ((error as FileProcessingError).type) {
      throw error;
    }
    throw {
      type: 'processing_failed',
      message: 'Could not process this image'
    } as FileProcessingError;
  }
}

/**
 * Process a document file into a FileAttachment.
 * Simple base64 conversion without resizing.
 */
async function processDocumentFile(file: File): Promise<FileAttachment> {
  // Validate file size
  if (file.size > FILE_CONSTRAINTS.document.MAX_SIZE) {
    throw {
      type: 'too_large',
      message: getSizeErrorMessage('document')
    } as FileProcessingError;
  }

  try {
    const dataUrl = await readFileAsDataUrl(file);
    const mimeType = inferMimeType(file.type, file.name);

    return {
      id: generateFileId(),
      type: 'document',
      dataUrl,
      mimeType,
      name: file.name,
      size: file.size
    };
  } catch {
    throw {
      type: 'processing_failed',
      message: 'Could not process this document'
    } as FileProcessingError;
  }
}

/**
 * Process a file into a FileAttachment.
 * Routes to appropriate handler based on file type.
 *
 * @throws {FileProcessingError} If validation fails or processing errors
 */
export async function processFile(file: File): Promise<FileAttachment> {
  const fileType = getFileType(file.type, file.name);

  if (!fileType) {
    throw {
      type: 'invalid_type',
      message: getTypeErrorMessage()
    } as FileProcessingError;
  }

  if (fileType === 'image') {
    return processImageFile(file);
  } else {
    return processDocumentFile(file);
  }
}


/**
 * Process multiple files into FileAttachments.
 * Returns successfully processed files and any errors.
 */
export async function processFiles(files: File[]): Promise<{
  files: FileAttachment[];
  errors: Array<{ file: File; error: FileProcessingError }>;
}> {
  const processed: FileAttachment[] = [];
  const errors: Array<{ file: File; error: FileProcessingError }> = [];

  for (const file of files) {
    try {
      const attachment = await processFile(file);
      processed.push(attachment);
    } catch (error) {
      errors.push({ file, error: error as FileProcessingError });
    }
  }

  return { files: processed, errors };
}


/**
 * Get all supported file types from a DataTransfer.
 */
export function getFilesFromDataTransfer(dataTransfer: DataTransfer): File[] {
  const files: File[] = [];

  for (let i = 0; i < dataTransfer.files.length; i++) {
    const file = dataTransfer.files[i];
    if (isFileSupported(file)) {
      files.push(file);
    }
  }

  return files;
}


/**
 * Extract files from clipboard data.
 */
export function getFilesFromClipboard(clipboardData: DataTransfer): File[] {
  const files: File[] = [];

  for (let i = 0; i < clipboardData.items.length; i++) {
    const item = clipboardData.items[i];
    if (item.kind === 'file') {
      const file = item.getAsFile();
      if (file && isFileSupported(file)) {
        files.push(file);
      }
    }
  }

  return files;
}


/**
 * Extract supported files from a drag-and-drop event's data transfer.
 */
export function getFilesFromDrop(dataTransfer: DataTransfer): File[] {
  return getFilesFromDataTransfer(dataTransfer);
}

/**
 * Format file size for display.
 */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Get file extension from filename.
 */
export function getFileExtension(filename: string): string {
  const parts = filename.split('.');
  return parts.length > 1 ? parts.pop()?.toUpperCase() || '' : '';
}

/**
 * Get supported file extensions for file input accept attribute.
 */
export function getSupportedFileExtensions(): string {
  const imageTypes = FILE_CONSTRAINTS.image.TYPES.join(',');
  const docTypes = FILE_CONSTRAINTS.document.TYPES.join(',');
  const extensionList = Object.keys(MIME_FALLBACK_BY_EXTENSION).join(',');
  return `${imageTypes},${docTypes},${extensionList}`;
}
