import threading
from tkinter import messagebox


def retry_file_states(gui, *, selected):
    transfer = getattr(gui, "_state_report_transfer", None)
    if transfer is None:
        return
    keys = None
    if selected:
        keys = [gui._state_rows[str(iid)]["key"] for iid in gui._state_table.selection()
                if str(iid) in gui._state_rows]
        if not keys:
            messagebox.showinfo("Retry", "Select failed files to retry.", parent=gui._state_window)
            return

    def report(message):
        if message:
            messagebox.showinfo("Retry", message, parent=gui.root)
        gui._refresh_transfer_states()

    def work():
        try:
            count = transfer.retry_failed_files(keys)
            if count and hasattr(transfer, "join"):
                transfer.join()
            message = None if count else "No selected files have a transient failure to retry."
        except Exception as error:
            message = str(error)
        # Queueing retries can block on bounded queues; keep it off the Tk thread.
        if hasattr(gui, "_ui_queue"):
            gui._ui_queue.put(("done", lambda _payload: report(message), None))
        else:
            gui._safe_after(lambda: report(message))

    threading.Thread(target=work, daemon=True).start()
