import { useState, useRef, useCallback } from 'react';
import {
  Upload,
  FileText,
  X,
  Clock,
  CheckCircle,
  AlertCircle,
  Loader2,
  Trash2,
  FolderArchive,
} from 'lucide-react';
import clsx from 'clsx';

interface PipelineResult {
  status: string;
  org_name?: string;
  total_uploaded?: number;
  valid_files?: number;
  unique_files?: number;
  documents_persisted?: number;
  elapsed_seconds?: number;
  error?: string;
}

interface DocumentUploadProps {
  onUploadComplete: () => void;
  onClearDatabase: () => Promise<void>;
}

export default function DocumentUpload({ onUploadComplete, onClearDatabase }: DocumentUploadProps) {
  const [files, setFiles] = useState<File[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [result, setResult] = useState<PipelineResult | null>(null);
  const [clearing, setClearing] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const addFiles = useCallback((incoming: FileList | File[]) => {
    const arr = Array.from(incoming);
    setFiles((prev) => {
      const existing = new Set(prev.map((f) => f.name + f.size));
      const unique = arr.filter((f) => !existing.has(f.name + f.size));
      return [...prev, ...unique];
    });
    setResult(null);
  }, []);

  const removeFile = useCallback((index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragOver(false);
      if (e.dataTransfer.files.length > 0) {
        addFiles(e.dataTransfer.files);
      }
    },
    [addFiles]
  );

  const startTimer = useCallback(() => {
    setElapsed(0);
    const start = Date.now();
    timerRef.current = setInterval(() => {
      setElapsed(Math.round((Date.now() - start) / 100) / 10);
    }, 100);
  }, []);

  const stopTimer = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const handleUpload = useCallback(async () => {
    if (files.length === 0) return;
    setUploading(true);
    setResult(null);
    startTimer();

    const formData = new FormData();
    files.forEach((f) => formData.append('files', f));

    try {
      const res = await fetch('/api/upload', { method: 'POST', body: formData });
      const data: PipelineResult = await res.json();
      stopTimer();
      setResult(data);
      if (data.status === 'ok') {
        setFiles([]);
        onUploadComplete();
      }
    } catch (err) {
      stopTimer();
      setResult({ status: 'error', error: String(err) });
    } finally {
      setUploading(false);
    }
  }, [files, onUploadComplete, startTimer, stopTimer]);

  const handleClear = useCallback(async () => {
    setClearing(true);
    try {
      await onClearDatabase();
      setConfirmClear(false);
      setResult(null);
      setFiles([]);
    } finally {
      setClearing(false);
    }
  }, [onClearDatabase]);

  const formatBytes = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const hasZip = files.some((f) => f.name.toLowerCase().endsWith('.zip'));

  return (
    <div className="bg-white rounded-lg shadow-sm border border-slate-200 overflow-hidden">
      {/* Card header */}
      <div className="flex items-center justify-between px-5 py-3 border-b border-slate-200 bg-slate-50/50">
        <div className="flex items-center gap-2">
          <Upload className="w-4 h-4 text-slate-500" />
          <h2 className="text-sm font-semibold text-slate-700">Document Ingestion</h2>
        </div>

        {/* Clear database button — inline in header */}
        {!confirmClear ? (
          <button
            type="button"
            onClick={() => setConfirmClear(true)}
            className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium text-slate-500 hover:text-red-600 hover:bg-red-50 rounded-md transition-colors cursor-pointer"
          >
            <Trash2 className="w-3.5 h-3.5" />
            Clear Database
          </button>
        ) : (
          <div className="flex items-center gap-2">
            <span className="text-xs text-red-600 font-medium">Delete all data?</span>
            <button
              type="button"
              onClick={handleClear}
              disabled={clearing}
              className="px-2.5 py-1 text-xs font-medium text-white bg-red-600 rounded-md hover:bg-red-700 disabled:opacity-50 transition-colors cursor-pointer"
            >
              {clearing ? 'Clearing...' : 'Confirm'}
            </button>
            <button
              type="button"
              onClick={() => setConfirmClear(false)}
              className="px-2.5 py-1 text-xs font-medium text-slate-500 bg-slate-100 rounded-md hover:bg-slate-200 transition-colors cursor-pointer"
            >
              Cancel
            </button>
          </div>
        )}
      </div>

      {/* Drop zone */}
      <div
        className={clsx(
          'mx-5 mt-4 mb-3 border-2 border-dashed rounded-lg p-5 text-center transition-all cursor-pointer',
          dragOver
            ? 'border-blue-400 bg-blue-50/50'
            : 'border-slate-200 hover:border-slate-300 hover:bg-slate-50/50',
          uploading && 'pointer-events-none opacity-50'
        )}
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        onClick={() => inputRef.current?.click()}
      >
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".pdf,.docx,.doc,.html,.htm,.xlsx,.xls,.png,.jpg,.jpeg,.zip,.msg"
          className="hidden"
          onChange={(e) => {
            if (e.target.files) addFiles(e.target.files);
            e.target.value = '';
          }}
        />
        <div className="flex items-center justify-center gap-3">
          <div className={clsx(
            'p-2.5 rounded-lg',
            dragOver ? 'bg-blue-100' : 'bg-slate-100'
          )}>
            <FolderArchive
              className={clsx('w-6 h-6', dragOver ? 'text-blue-500' : 'text-slate-400')}
            />
          </div>
          <div className="text-left">
            <p className="text-sm font-medium text-slate-700">
              Drop files or a <span className="text-blue-600">.zip</span> organization folder
            </p>
            <p className="text-xs text-slate-400 mt-0.5">
              PDF, DOCX, HTML, XLSX, PNG, JPG, ZIP, MSG
            </p>
          </div>
        </div>
      </div>

      {/* File list */}
      {files.length > 0 && (
        <div className="mx-5 mb-3">
          <div className="border border-slate-200 rounded-lg overflow-hidden">
            <div className="px-4 py-2.5 bg-slate-50 border-b border-slate-200 flex items-center justify-between">
              <span className="text-xs font-medium text-slate-600">
                {files.length} file{files.length > 1 ? 's' : ''} selected
                <span className="text-slate-400 ml-1.5">
                  ({formatBytes(files.reduce((sum, f) => sum + f.size, 0))})
                </span>
                {hasZip && (
                  <span className="ml-2 inline-flex items-center gap-1 text-blue-600">
                    <FolderArchive className="w-3 h-3" />
                    zip
                  </span>
                )}
              </span>
              <button
                type="button"
                onClick={handleUpload}
                disabled={uploading}
                className={clsx(
                  'flex items-center gap-1.5 px-3 py-1 text-xs font-semibold rounded-md transition-colors',
                  uploading
                    ? 'bg-blue-400 text-white cursor-not-allowed'
                    : 'bg-blue-600 text-white hover:bg-blue-700'
                )}
              >
                {uploading ? (
                  <>
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    Processing
                  </>
                ) : (
                  <>
                    <Upload className="w-3.5 h-3.5" />
                    Upload & Process
                  </>
                )}
              </button>
            </div>

            <ul className="divide-y divide-slate-100 max-h-40 overflow-y-auto">
              {files.map((file, i) => (
                <li key={file.name + file.size} className="flex items-center gap-3 px-4 py-2">
                  <FileText className="w-3.5 h-3.5 text-slate-400 shrink-0" />
                  <span className="text-xs text-slate-700 truncate flex-1">{file.name}</span>
                  <span className="text-xs text-slate-400 tabular-nums shrink-0">
                    {formatBytes(file.size)}
                  </span>
                  {!uploading && (
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        removeFile(i);
                      }}
                      className="text-slate-300 hover:text-red-500 transition-colors"
                    >
                      <X className="w-3.5 h-3.5" />
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}

      {/* Processing timer */}
      {uploading && (
        <div className="mx-5 mb-3 flex items-center gap-3 px-4 py-2.5 bg-blue-50 border border-blue-200 rounded-lg">
          <Loader2 className="w-4 h-4 text-blue-600 animate-spin shrink-0" />
          <div className="flex-1 min-w-0">
            <p className="text-xs font-medium text-blue-800">Processing documents...</p>
            <p className="text-xs text-blue-500 truncate">
              Validating, deduplicating, and persisting
            </p>
          </div>
          <div className="flex items-center gap-1 text-xs font-mono font-semibold text-blue-700 bg-blue-100 px-2 py-0.5 rounded shrink-0">
            <Clock className="w-3 h-3" />
            {elapsed.toFixed(1)}s
          </div>
        </div>
      )}

      {/* Result */}
      {result && !uploading && (
        <div
          className={clsx(
            'mx-5 mb-3 flex items-center gap-3 px-4 py-2.5 rounded-lg border',
            result.status === 'ok'
              ? 'bg-emerald-50 border-emerald-200'
              : 'bg-red-50 border-red-200'
          )}
        >
          {result.status === 'ok' ? (
            <CheckCircle className="w-4 h-4 text-emerald-600 shrink-0" />
          ) : (
            <AlertCircle className="w-4 h-4 text-red-600 shrink-0" />
          )}
          <div className="flex-1 min-w-0">
            {result.status === 'ok' ? (
              <p className="text-xs text-emerald-800">
                <span className="font-semibold">{result.org_name}</span>
                {' — '}
                {result.documents_persisted} doc{result.documents_persisted !== 1 ? 's' : ''} persisted
                <span className="text-emerald-600 ml-1">
                  ({result.total_uploaded} uploaded, {result.valid_files} valid, {result.unique_files} unique)
                </span>
              </p>
            ) : (
              <p className="text-xs text-red-700">{result.error}</p>
            )}
          </div>
          <span className="flex items-center gap-1 text-xs font-mono text-slate-400 shrink-0">
            <Clock className="w-3 h-3" />
            {result.elapsed_seconds?.toFixed(1)}s
          </span>
        </div>
      )}

      {/* Bottom padding when nothing below the drop zone */}
      {files.length === 0 && !uploading && !result && <div className="h-2" />}
    </div>
  );
}
