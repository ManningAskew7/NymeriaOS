/**
 * File processing utilities for handling file uploads.
 * Validates, resizes images, and converts files to base64 for multimodal messages.
 */

import type { FileAttachment, FileType } from '$lib/types';

// File constraints by type
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
  },
  MAX_FILES_PER_MESSAGE: 4
};

export type SupportedImageType = typeof FILE_CONSTRAINTS.image.TYPES[number];
export type SupportedDocumentType = typeof FILE_CONSTRAINTS.document.TYPES[number];

export interface FileProcessingError {
  type: 'invalid_type' | 'too_large' | 'processing_failed';
  message: string;
}


/**
 * Determine the file type category from a MIME type.
 */
export function getFileType(mimeType: string): FileType | null {
  if ((FILE_CONSTRAINTS.image.TYPES as readonly string[]).includes(mimeType)) {
    return 'image';
  }
  if ((FILE_CONSTRAINTS.document.TYPES as readonly string[]).includes(mimeType)) {
    return 'document';
  }
  return null;
}

/**
 * Check if a file is a supported type.
 */
export function isFileSupported(file: File): boolean {
  return getFileType(file.type) !== null;
}

/**
 * Validate that a file is a supported image type.
 */
export function validateImageType(file: File): boolean {
  return getFileType(file.type) === 'image';
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
 * Get a user-friendly error message for too many files.
 */
export function getMaxFilesErrorMessage(): string {
  return `Maximum ${FILE_CONSTRAINTS.MAX_FILES_PER_MESSAGE} files per message`;
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
    let mimeType = file.type;

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

    return {
      id: generateFileId(),
      type: 'document',
      dataUrl,
      mimeType: file.type,
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
  const fileType = getFileType(file.type);

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
  return `${imageTypes},${docTypes}`;
}
