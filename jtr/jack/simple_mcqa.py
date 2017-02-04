from jtr.jack import *
import tensorflow as tf

from jtr.jack.data_structures import *
from jtr.pipelines import pipeline, deep_seq_map, deep_map
from jtr.preprocess.batch import get_batches, GeneratorWithRestart
from jtr.preprocess.vocab import Vocab


class SimpleMCInputModule(InputModule):
    def __init__(self, shared_resources):
        self.vocab = shared_resources.vocab
        self.config = shared_resources.config
        self.shared_resources = shared_resources

    def setup_from_data(self, data: List[Tuple[Question, List[Answer]]]) -> SharedResources:
        self.preprocess(data)
        return self.shared_resources

    def setup(self, shared_resources: SharedResources):
        pass

    @property
    def training_ports(self) -> List[TensorPort]:
        return [Ports.Targets.candidate_labels]

    def preprocess(self, data, test_time=False):
        corpus = {"support": [], "question": [], "candidates": []}
        if not test_time:
            corpus["answers"] = []
        for xy in data:
            if test_time:
                x = xy
                y = None
            else:
                x, y = xy
            corpus["support"].append(x.support)
            corpus["question"].append(x.question)
            corpus["candidates"].append(x.atomic_candidates)
            if not test_time:
                corpus["answers"].append([y.text])
        corpus, _, _, _ = pipeline(corpus, self.vocab, sepvocab=False,
                                   test_time=test_time)
        return corpus

    def dataset_generator(self, dataset: List[Tuple[Question, Answer]], is_eval: bool) \
            -> Iterable[Mapping[TensorPort, np.ndarray]]:
        corpus, _ = self.preprocess(dataset)
        xy_dict = {
            Ports.Input.multiple_support: corpus["support"],
            Ports.Input.question: corpus["question"],
            Ports.Input.atomic_candidates: corpus["candidates"],
            Ports.Targets.candidate_labels: corpus["answers"]
        }
        return get_batches(xy_dict)

    def __call__(self, inputs: List[Question]) -> Mapping[TensorPort, np.ndarray]:
        corpus = self.preprocess(inputs, test_time=True)
        x_dict = {
            Ports.Input.multiple_support: corpus["support"],
            Ports.Input.question: corpus["question"],
            Ports.Input.atomic_candidates: corpus["candidates"]
        }
        return x_dict

    @property
    def output_ports(self) -> List[TensorPort]:
        return [Ports.Input.multiple_support,
                Ports.Input.question, Ports.Input.atomic_candidates]


class SimpleMCModelModule(SimpleModelModule):
    def __init__(self, vocab: SharedVocabAndConfig):
        self.vocab = vocab.vocab
        self.embeddings = None
        super().__init__()

    @property
    def output_ports(self) -> List[TensorPort]:
        return [Ports.Prediction.candidate_scores]

    @property
    def training_output_ports(self) -> List[TensorPort]:
        return [Ports.loss]

    @property
    def training_input_ports(self) -> List[TensorPort]:
        return self.input_ports + [Ports.Targets.candidate_labels]

    @property
    def input_ports(self) -> List[TensorPort]:
        return [Ports.Input.multiple_support, Ports.Input.question, Ports.Input.atomic_candidates]

    def create_training_output(self,
                               shared_resources: SharedVocabAndConfig,
                               multiple_support: tf.Tensor,
                               question: tf.Tensor,
                               atomic_candidates: tf.Tensor,
                               candidate_labels: tf.Tensor) -> Mapping[TensorPort, tf.Tensor]:
        output = self.create_output(shared_resources, multiple_support, question, atomic_candidates)
        scores = output[Ports.Prediction.candidate_scores]  # [num_batches,num_candidates]
        loss = tf.nn.softmax_cross_entropy_with_logits(scores, candidate_labels)

        return {
            Ports.loss: loss,
            **output
        }

    def create_output(self, shared_resources: SharedVocabAndConfig,
                      multiple_support: tf.Tensor,
                      question: tf.Tensor,
                      atomic_candidates: tf.Tensor) -> Mapping[TensorPort, tf.Tensor]:
        emb_dim = shared_resources.config["emb_dim"]
        embeddings = tf.get_variable(
            "embeddings", [len(self.vocab), emb_dim],
            trainable=True, dtype="float32")

        embedded_supports = tf.reduce_sum(tf.gather(embeddings, multiple_support), (1, 2))  # [batch_size, emb_dim]
        embedded_question = tf.reduce_sum(tf.gather(embeddings, question), (1,))  # [batch_size, emb_dim]
        embedded_supports_and_question = embedded_supports + embedded_question
        embedded_candidates = tf.gather(embeddings, question)  # [batch_size, num_candidates, emb_dim]

        scores = tf.einsum("bce,be->bc", embedded_candidates, embedded_supports_and_question)

        return {
            Ports.Prediction.candidate_scores: scores
        }


class ExampleOutputModule(OutputModule):
    def setup(self, shared_resources: SharedResources):
        pass

    @property
    def input_ports(self) -> List[TensorPort]:
        return [Ports.Prediction.candidate_scores]

    def __call__(self, inputs: List[Question], candidate_scores: np.ndarray) -> List[Answer]:
        # len(inputs) == batch size
        # candidate_scores: [batch_size, max_num_candidates]
        winning_indices = np.argmax(candidate_scores, axis=1)
        result = []
        for index_in_batch, question in enumerate(inputs):
            winning_index = winning_indices[index_in_batch]
            score = candidate_scores[index_in_batch, winning_index]
            result.append(AnswerWithDefault(question.candidates[winning_index], score=score))
        return result


if __name__ == '__main__':
    data_set = [
        (QuestionWithDefaults("which is it?", ["a is true", "b isn't"], atomic_candidates=["a", "b", "c"]),
         AnswerWithDefault("a", score=1.0))
    ]

    resources = SharedVocabAndConfig(Vocab(), {"emb_dim": 10})
    example_reader = JTReader(SimpleMCInputModule(resources),
                              SimpleMCModelModule(resources),
                              ExampleOutputModule(),
                              resources)

    example_reader.setup_from_data(data_set)

    # todo: chose optimizer based on config
    example_reader.train(tf.train.AdamOptimizer(), data_set)

    # answers = example_reader(data_set)