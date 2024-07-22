from dataclasses import dataclass

@dataclass
class Shard:
    model_id: str
    start_layer: int
    end_layer: int
    n_layers: int

    def is_first_layer(self) -> bool:
        return self.start_layer == 0

    def is_last_layer(self) -> bool:
        return self.end_layer == self.n_layers - 1

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "start_layer": self.start_layer,
            "end_layer": self.end_layer,
            "n_layers": self.n_layers
        }
