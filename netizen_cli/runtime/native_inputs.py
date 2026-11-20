"""Render prepared Channel material into public Codex SDK inputs."""

from openai_codex import ImageInput, TextInput

from ..prompt_inputs import PreparedPrompt


def to_codex_input(prompt: PreparedPrompt) -> str | list[TextInput | ImageInput]:
    if not prompt.images:
        return prompt.text
    items: list[TextInput | ImageInput] = []
    for image in prompt.images:
        items.append(TextInput(image.label))
        items.append(ImageInput(image.data_url))
    items.append(TextInput(prompt.text))
    return items
