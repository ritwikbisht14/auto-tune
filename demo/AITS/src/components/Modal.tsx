import { useEffect, useRef, type ReactNode } from "react";

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
}

export function Modal({ open, onClose, title, children }: ModalProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open) dialog.showModal();
    else dialog.close();
  }, [open]);

  return (
    <dialog ref={dialogRef} onCancel={onClose} aria-labelledby="modal-title">
      <header>
        <h2 id="modal-title">{title}</h2>
        <button onClick={onClose} aria-label="Close dialog">×</button>
      </header>
      <div>{children}</div>
    </dialog>
  );
}
