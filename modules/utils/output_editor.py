import os
import re
from typing import Dict, List, Optional

import gradio as gr
from gradio_i18n import gettext as _

# Must match how BaseTranscriptionPipeline.transcribe_* joins per-file results in the output textbox
FILE_SEPARATOR = '------------------------------------\n'
SPEAKER_LABEL_PATTERN = re.compile(r"(?m)(?:^|(?<=\]))(SPEAKER_\d+|None)\|")


def split_output(text: str, expected_files: int) -> List[str]:
    """Split the output textbox back into each file's content, in the same order as the output files."""
    chunks = text.split(FILE_SEPARATOR)[1:]
    if len(chunks) != expected_files:
        raise gr.Error(_("Couldn't match the edited text to the output files. "
                         "Keep the '-----' separator lines and file name lines in place."))
    contents = []
    for chunk in chunks:
        # chunk is "<file name>\n\n<file content>"
        _name, sep, content = chunk.partition("\n\n")
        if not sep:
            raise gr.Error(_("Couldn't find the file name line in the edited text. "
                             "Keep the file name and the blank line after it."))
        contents.append(content)
    return contents


def resolve_output_paths(cached_files: Optional[List[str]], output_dir: str) -> List[str]:
    """gr.Files hands back Gradio's cached copies. Map them to the real files in the outputs folder."""
    if not cached_files:
        return []
    paths = []
    for f in cached_files:
        path = f if isinstance(f, str) else f.name
        original = os.path.join(output_dir, os.path.basename(path))
        paths.append(original if os.path.exists(original) else path)
    return paths


def find_speaker_labels(text: str) -> List[str]:
    labels = set(SPEAKER_LABEL_PATTERN.findall(text))
    return sorted(labels, key=lambda label: (label == "None", label))


def rename_speakers(text: str, current_names: Dict[str, str], new_names: Dict[str, str]) -> str:
    """
    Replace speaker labels at the start of subtitle lines.
    `current_names` maps each original label to what is shown now, so names can be changed repeatedly.
    """
    shown_to_label = {shown: label for label, shown in current_names.items()}
    if not shown_to_label:
        return text
    pattern = re.compile(
        r"(?m)(?:^|(?<=\]))(" + "|".join(re.escape(s) for s in sorted(shown_to_label, key=len, reverse=True)) + r")\|"
    )
    return pattern.sub(lambda m: new_names[shown_to_label[m.group(1)]] + "|", text)


class OutputEditor:
    """
    Makes the output textbox editable. Edits and speaker names are written straight back to the
    output files, and the download links are refreshed, so downloads always match the textbox.
    """

    def __init__(self, output_dir: str):
        self.output_dir = output_dir

    def create_ui(self, tb_output: gr.Textbox, files_output: gr.Files, run_event):
        state_paths = gr.State([])
        state_names = gr.State({})  # original label -> name currently shown in the text

        with gr.Accordion(_("Speaker Names"), open=True, visible=False) as acc_speakers:
            df_speakers = gr.Dataframe(
                headers=[_("Speaker Label"), _("Name")],
                datatype=["str", "str"],
                col_count=(2, "fixed"),
                static_columns=[0],
                type="array",
                interactive=True,
            )

        run_event.then(
            fn=self.on_transcribed,
            inputs=[tb_output, files_output],
            outputs=[state_paths, state_names, df_speakers, acc_speakers],
        )
        # "always_last" collapses a burst of keystrokes into a single write
        tb_output.input(
            fn=self.write_files,
            inputs=[tb_output, state_paths],
            outputs=[files_output],
            trigger_mode="always_last",
            show_progress="hidden",
        )
        df_speakers.input(
            fn=self.apply_names,
            inputs=[tb_output, df_speakers, state_names, state_paths],
            outputs=[tb_output, files_output, state_names],
            trigger_mode="always_last",
            show_progress="hidden",
        )

    def on_transcribed(self, text: str, files: Optional[List[str]]):
        paths = resolve_output_paths(files, self.output_dir)
        labels = find_speaker_labels(text or "")
        names = {label: label for label in labels}
        return paths, names, [[label, ""] for label in labels], gr.update(visible=bool(labels))

    def write_files(self, text: str, paths: List[str]):
        if not paths:
            return gr.skip()
        try:
            contents = split_output(text, len(paths))
        except gr.Error as e:
            # Don't overwrite files with a half-broken split; tell the user why edits aren't landing
            gr.Warning(e.message)
            return gr.skip()
        for path, content in zip(paths, contents):
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        return paths

    def apply_names(self, text: str, rows, current_names: Dict[str, str], paths: List[str]):
        if not current_names:
            return gr.skip(), gr.skip(), gr.skip()
        new_names = dict(current_names)
        for label, name in (rows or []):
            name = (name or "").strip().replace("|", "/")
            if label in new_names:
                new_names[label] = name or label
        text = rename_speakers(text, current_names, new_names)
        return text, self.write_files(text, paths), new_names
