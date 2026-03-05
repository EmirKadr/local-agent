import tkinter as tk
from tkinter import ttk, messagebox
import shutil
import os
from pathlib import Path
from PIL import Image, ImageTk

BILDER_DIR = Path("bilder")
RESULTS_DIR = Path("resultat")


class ImageClassifierApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Bildklassificerare")
        self.geometry("800x600")
        self.resizable(True, True)

        self.test_name = ""
        self.categories = []
        self.images = []
        self.current_index = 0

        self._show_setup_screen()

    # ------------------------------------------------------------------ #
    #  SCREEN 1 – Setup                                                    #
    # ------------------------------------------------------------------ #

    def _show_setup_screen(self):
        self._clear()

        frame = tk.Frame(self, padx=30, pady=30)
        frame.pack(fill="both", expand=True)

        tk.Label(frame, text="Bildklassificerare", font=("Helvetica", 20, "bold")).pack(pady=(0, 20))

        # Test name
        name_frame = tk.Frame(frame)
        name_frame.pack(fill="x", pady=5)
        tk.Label(name_frame, text="Testets namn:", width=18, anchor="w").pack(side="left")
        self.name_var = tk.StringVar()
        tk.Entry(name_frame, textvariable=self.name_var, width=30).pack(side="left")

        # Category rows
        tk.Label(frame, text="Kategorier:", font=("Helvetica", 12), anchor="w").pack(anchor="w", pady=(20, 5))

        self.cat_frame = tk.Frame(frame)
        self.cat_frame.pack(fill="x")

        self.cat_vars = []
        for _ in range(3):
            self._add_category_row()

        btn_row = tk.Frame(frame)
        btn_row.pack(pady=10)
        tk.Button(btn_row, text="+ Lägg till kategori", command=self._add_category_row).pack(side="left", padx=5)
        tk.Button(btn_row, text="- Ta bort sista", command=self._remove_last_row).pack(side="left", padx=5)

        tk.Button(
            frame,
            text="Starta test  ▶",
            font=("Helvetica", 13, "bold"),
            bg="#4CAF50",
            fg="white",
            padx=15,
            pady=8,
            command=self._start_test,
        ).pack(pady=20)

    def _add_category_row(self):
        row = tk.Frame(self.cat_frame)
        row.pack(fill="x", pady=2)
        var = tk.StringVar()
        idx = len(self.cat_vars) + 1
        tk.Label(row, text=f"Kategori {idx}:", width=12, anchor="w").pack(side="left")
        tk.Entry(row, textvariable=var, width=25).pack(side="left")
        self.cat_vars.append(var)

    def _remove_last_row(self):
        if len(self.cat_vars) <= 1:
            return
        self.cat_vars.pop()
        for widget in self.cat_frame.winfo_children():
            widget.destroy()
        tmp = list(self.cat_vars)
        self.cat_vars = []
        for var in tmp:
            self._add_category_row_with_var(var)

    def _add_category_row_with_var(self, var):
        row = tk.Frame(self.cat_frame)
        row.pack(fill="x", pady=2)
        idx = len(self.cat_vars) + 1
        tk.Label(row, text=f"Kategori {idx}:", width=12, anchor="w").pack(side="left")
        tk.Entry(row, textvariable=var, width=25).pack(side="left")
        self.cat_vars.append(var)

    def _start_test(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning("Saknar testnamn", "Ange ett namn för testet.")
            return

        categories = [v.get().strip() for v in self.cat_vars if v.get().strip()]
        if not categories:
            messagebox.showwarning("Saknar kategorier", "Ange minst en kategori.")
            return

        if not BILDER_DIR.exists() or not any(BILDER_DIR.iterdir()):
            messagebox.showwarning(
                "Inga bilder",
                f"Mappen '{BILDER_DIR}' saknas eller är tom.\nLägg bilder där och starta om.",
            )
            return

        images = sorted(
            p for p in BILDER_DIR.iterdir()
            if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
        )
        if not images:
            messagebox.showwarning("Inga bilder", "Inga bildfiler hittades i mappen 'bilder'.")
            return

        self.test_name = name
        self.categories = categories
        self.images = images
        self.current_index = 0

        RESULTS_DIR.mkdir(exist_ok=True)

        self._show_classify_screen()

    # ------------------------------------------------------------------ #
    #  SCREEN 2 – Classify                                                 #
    # ------------------------------------------------------------------ #

    def _show_classify_screen(self):
        self._clear()

        # Top bar
        top = tk.Frame(self, padx=10, pady=5, bg="#222")
        top.pack(fill="x")
        tk.Label(top, text=f"Test: {self.test_name}", fg="white", bg="#222", font=("Helvetica", 11)).pack(side="left")
        self.progress_label = tk.Label(top, text="", fg="white", bg="#222", font=("Helvetica", 11))
        self.progress_label.pack(side="right")

        # Image area
        self.img_frame = tk.Frame(self, bg="#111")
        self.img_frame.pack(fill="both", expand=True, padx=10, pady=10)

        self.img_label = tk.Label(self.img_frame, bg="#111")
        self.img_label.pack(expand=True)

        self.filename_label = tk.Label(self, text="", font=("Helvetica", 9), fg="#555")
        self.filename_label.pack()

        # Category buttons
        btn_outer = tk.Frame(self, pady=10)
        btn_outer.pack()

        btn_frame = tk.Frame(btn_outer)
        btn_frame.pack()

        all_cats = self.categories + ["Övrigt"]
        colors = ["#2196F3", "#E91E63", "#FF9800", "#9C27B0", "#00BCD4", "#4CAF50", "#F44336", "#795548"]

        for i, cat in enumerate(all_cats):
            is_ovrigt = cat == "Övrigt"
            color = "#607D8B" if is_ovrigt else colors[i % len(colors)]
            tk.Button(
                btn_frame,
                text=cat,
                font=("Helvetica", 12, "bold"),
                bg=color,
                fg="white",
                padx=18,
                pady=10,
                relief="flat",
                command=lambda c=cat: self._classify(c),
            ).pack(side="left", padx=6)

        # End test button
        tk.Button(
            self,
            text="Avsluta test",
            bg="#b71c1c",
            fg="white",
            font=("Helvetica", 10),
            padx=10,
            pady=5,
            command=self._end_test,
        ).pack(pady=(0, 12))

        self._load_current_image()

    def _load_current_image(self):
        if self.current_index >= len(self.images):
            self._end_test(all_done=True)
            return

        path = self.images[self.current_index]
        self.filename_label.config(text=path.name)
        self.progress_label.config(
            text=f"Bild {self.current_index + 1} / {len(self.images)}"
        )

        try:
            img = Image.open(path)
            # Scale to fit ~560×400 while keeping aspect ratio
            img.thumbnail((560, 400), Image.LANCZOS)
            self._tk_image = ImageTk.PhotoImage(img)
            self.img_label.config(image=self._tk_image, text="")
        except Exception as e:
            self.img_label.config(image="", text=f"[Kan inte visa bild: {e}]", fg="red")

    def _classify(self, category):
        src = self.images[self.current_index]
        # Folder name: testname.category  (clean up forbidden chars)
        safe_cat = category.replace(" ", "_").replace("/", "-").replace("\\", "-")
        dest_dir = RESULTS_DIR / f"{self.test_name}.{safe_cat}"
        dest_dir.mkdir(parents=True, exist_ok=True)

        dest = dest_dir / src.name
        # Avoid overwrite: append suffix if needed
        if dest.exists():
            stem = src.stem
            suffix = src.suffix
            counter = 1
            while dest.exists():
                dest = dest_dir / f"{stem}_{counter}{suffix}"
                counter += 1

        shutil.copy2(src, dest)

        self.current_index += 1
        self._load_current_image()

    def _end_test(self, all_done=False):
        done = self.current_index
        if all_done:
            msg = f"Alla {done} bilder är klassificerade!\nResultat sparade i mappen 'resultat'."
        else:
            remaining = len(self.images) - done
            msg = (
                f"Testet avslutat.\n"
                f"Klassificerade: {done} bilder\n"
                f"Återstår: {remaining} bilder\n\n"
                f"Resultaten sparas i 'resultat/{self.test_name}.*'"
            )
        messagebox.showinfo("Test avslutat", msg)
        self._show_setup_screen()

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _clear(self):
        for widget in self.winfo_children():
            widget.destroy()


if __name__ == "__main__":
    app = ImageClassifierApp()
    app.mainloop()
