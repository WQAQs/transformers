import logging
import os


from transformers import ElectraConfig

from .modeling_tf_bert import TFBertEmbeddings, TFBertEncoder, TFBertPreTrainedModel, ACT2FN
import tensorflow as tf
from .modeling_tf_utils import TFPreTrainedModel, get_initializer, keras_serializable, shape_list


logger = logging.getLogger(__name__)


TF_ELECTRA_PRETRAINED_MODEL_ARCHIVE_MAP = {
    "google/electra-small-generator": "https://s3.amazonaws.com/models.huggingface.co/bert/google/electra-small-generator/pytorch_model.bin",
    "google/electra-base-generator": "https://s3.amazonaws.com/models.huggingface.co/bert/google/electra-base-generator/pytorch_model.bin",
    "google/electra-large-generator": "https://s3.amazonaws.com/models.huggingface.co/bert/google/electra-large-generator/pytorch_model.bin",
    "google/electra-small-discriminator": "https://s3.amazonaws.com/models.huggingface.co/bert/google/electra-small-discriminator/pytorch_model.bin",
    "google/electra-base-discriminator": "https://s3.amazonaws.com/models.huggingface.co/bert/google/electra-base-discriminator/pytorch_model.bin",
    "google/electra-large-discriminator": "https://s3.amazonaws.com/models.huggingface.co/bert/google/electra-large-discriminator/pytorch_model.bin",
}



class TFElectraEmbeddings(TFBertEmbeddings):
    def __init__(self, config):
        super().__init__(config)
        self.vocab_size = config.vocab_size
        self.initializer_range = config.initializer_range
        self.embedding_size = config.embedding_size

        self.position_embeddings = tf.keras.layers.Embedding(
            config.max_position_embeddings,
            config.embedding_size,
            embeddings_initializer=get_initializer(self.initializer_range),
            name="position_embeddings",
        )
        self.token_type_embeddings = tf.keras.layers.Embedding(
            config.type_vocab_size,
            config.embedding_size,
            embeddings_initializer=get_initializer(self.initializer_range),
            name="token_type_embeddings",
        )

    def build(self, input_shape):
        """Build shared word embedding layer """
        with tf.name_scope("word_embeddings"):
            # Create and initialize weights. The random normal initializer was chosen
            # arbitrarily, and works well.
            self.word_embeddings = self.add_weight(
                "weight",
                shape=[self.vocab_size, self.embedding_size],
                initializer=get_initializer(self.initializer_range),
            )
        super().build(input_shape)


class TFElectraDiscriminatorPredictions(tf.keras.layers.Layer):
    def __init__(self, config):
        super().__init__()

        self.dense = tf.keras.layers.Dense(config.hidden_size)
        self.dense_prediction = tf.keras.layers.Dense(1)
        self.config = config

    def forward(self, discriminator_hidden_states, attention_mask):
        hidden_states = self.dense(discriminator_hidden_states)
        hidden_states = ACT2FN[self.config.hidden_act](hidden_states)
        logits = tf.squeeze(self.dense_prediction(hidden_states))

        return logits


class TFElectraGeneratorPredictions(tf.keras.layers.Layer):
    def __init__(self, config):
        super().__init__()

        self.LayerNorm = tf.keras.layers.LayerNormalization(epsilon=config.layer_norm_eps, name="LayerNorm")
        self.dense = tf.keras.layers.Dense(config.embedding_size)

    def forward(self, generator_hidden_states):
        hidden_states = self.dense(generator_hidden_states)
        hidden_states = ACT2FN["gelu"](hidden_states)
        hidden_states = self.LayerNorm(hidden_states)

        return hidden_states


class TFElectraPreTrainedModel(TFBertPreTrainedModel):

    config_class = ElectraConfig
    pretrained_model_archive_map = TF_ELECTRA_PRETRAINED_MODEL_ARCHIVE_MAP
    base_model_prefix = "electra"

    def get_extended_attention_mask(self, attention_mask, input_shape):
        if attention_mask is None:
            attention_mask = tf.fill(input_shape, 1)

        # We create a 3D attention mask from a 2D tensor mask.
        # Sizes are [batch_size, 1, 1, to_seq_length]
        # So we can broadcast to [batch_size, num_heads, from_seq_length, to_seq_length]
        # this attention mask is more simple than the triangular masking of causal attention
        # used in OpenAI GPT, we just need to prepare the broadcast dimension here.
        extended_attention_mask = attention_mask[:, tf.newaxis, tf.newaxis, :]

        # Since attention_mask is 1.0 for positions we want to attend and 0.0 for
        # masked positions, this operation will create a tensor which is 0.0 for
        # positions we want to attend and -10000.0 for masked positions.
        # Since we are adding it to the raw scores before the softmax, this is
        # effectively the same as removing these entirely.

        extended_attention_mask = tf.cast(extended_attention_mask, tf.float32)
        extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0

        return extended_attention_mask

    def get_head_mask(self, head_mask):
        if head_mask is not None:
            raise NotImplementedError
        else:
            head_mask = [None] * self.config.num_hidden_layers

        return head_mask


class TFElectraModel(TFElectraPreTrainedModel):

    config_class = ElectraConfig
    base_model_prefix = "discriminator"

    def __init__(self, config):
        super().__init__(config)
        self.embeddings = TFElectraEmbeddings(config)
        self.embeddings_project = tf.keras.layers.Dense(config.hidden_size)
        self.encoder = TFBertEncoder(config)
        self.config = config

    def get_input_embeddings(self):
        return self.embeddings.word_embeddings

    def _resize_token_embeddings(self, new_num_tokens):
        raise NotImplementedError

    def _prune_heads(self, heads_to_prune):
        """ Prunes heads of the model.
            heads_to_prune: dict of {layer_num: list of heads to prune in this layer}
            See base class PreTrainedModel
        """
        raise NotImplementedError

    def call(
            self,
            inputs,
            attention_mask=None,
            token_type_ids=None,
            position_ids=None,
            head_mask=None,
            inputs_embeds=None,
            training=False,
    ):
        if isinstance(inputs, (tuple, list)):
            input_ids = inputs[0]
            attention_mask = inputs[1] if len(inputs) > 1 else attention_mask
            token_type_ids = inputs[2] if len(inputs) > 2 else token_type_ids
            position_ids = inputs[3] if len(inputs) > 3 else position_ids
            head_mask = inputs[4] if len(inputs) > 4 else head_mask
            inputs_embeds = inputs[5] if len(inputs) > 5 else inputs_embeds
            assert len(inputs) <= 6, "Too many inputs."
        elif isinstance(inputs, dict):
            input_ids = inputs.get("input_ids")
            attention_mask = inputs.get("attention_mask", attention_mask)
            token_type_ids = inputs.get("token_type_ids", token_type_ids)
            position_ids = inputs.get("position_ids", position_ids)
            head_mask = inputs.get("head_mask", head_mask)
            inputs_embeds = inputs.get("inputs_embeds", inputs_embeds)
            assert len(inputs) <= 6, "Too many inputs."
        else:
            input_ids = inputs

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            input_shape = shape_list(input_ids)
        elif inputs_embeds is not None:
            input_shape = shape_list(inputs_embeds)[:-1]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        if attention_mask is None:
            attention_mask = tf.fill(input_shape, 1)
        if token_type_ids is None:
            token_type_ids = tf.fill(input_shape, 0)

        extended_attention_mask = self.get_extended_attention_mask(attention_mask, input_shape)
        head_mask = self.get_head_mask(head_mask)

        hidden_states = self.embeddings([input_ids, position_ids, token_type_ids, inputs_embeds], training=training)

        if hidden_states.shape[-1] != self.config.hidden_size:
            hidden_states = self.embeddings_project(hidden_states, training=training)

        hidden_states = self.encoder(hidden_states, attention_mask=extended_attention_mask, head_mask=head_mask, training=training)

        return hidden_states


class TFElectraForMaskedLM(TFElectraPreTrainedModel):

    base_model_prefix = "generator"

    def __init__(self, config, input_embeddings, **kwargs):
        super().__init__(config, **kwargs)

        self.vocab_size = config.vocab_size
        self.generator = TFElectraModel(config)
        self.generator_predictions = TFElectraGeneratorPredictions(config)
        if isinstance(config.hidden_act, str):
            self.activation = ACT2FN[config.hidden_act]
        else:
            self.activation = config.hidden_act
        self.generator_lm_head = input_embeddings

    def get_output_embeddings(self):
        return self.generator_lm_head

    def build(self, input_shape):
        self.generator_lm_head_bias = self.add_weight(
            shape=(self.vocab_size,), initializer="zeros", trainable=True, name="generator_lm_head/bias"
        )
        super().build(input_shape)

    def call(
            self,
            input_ids=None,
            attention_mask=None,
            token_type_ids=None,
            position_ids=None,
            head_mask=None,
            inputs_embeds=None,
            training=False
    ):

        generator_hidden_states = self.generator(
            input_ids, attention_mask, token_type_ids, position_ids, head_mask, inputs_embeds, training=training
        )
        generator_sequence_output = generator_hidden_states[0]
        prediction_scores = self.generator_predictions(generator_sequence_output, training=training)
        prediction_scores = self.generator_lm_head(prediction_scores, training=training, mode="linear")
        output = (prediction_scores,)
        output += generator_hidden_states[1:]

        return output  # (masked_lm_loss), prediction_scores, (hidden_states), (attentions)


class TFElectraForPreTraining(TFElectraPreTrainedModel):

    base_model_prefix = "discriminator"

    def __init__(self, config):
        super().__init__(config)

        self.discriminator = TFElectraModel(config)
        self.discriminator_predictions = TFElectraDiscriminatorPredictions(config)

    def call(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        training=False
    ):

        discriminator_hidden_states = self.discriminator(
            input_ids, attention_mask, token_type_ids, position_ids, head_mask, inputs_embeds, training=training
        )
        discriminator_sequence_output = discriminator_hidden_states[0]
        logits = self.discriminator_predictions(discriminator_sequence_output, attention_mask)
        output = (logits,)
        output += discriminator_hidden_states[1:]

        return output  # (loss), scores, (hidden_states), (attentions)


class TFElectraForTokenClassification(TFElectraPreTrainedModel):

    base_model_prefix = "discriminator"

    def __init__(self, config):
        super().__init__(config)

        self.discriminator = TFElectraModel(config)
        self.dropout = tf.keras.layers.Dropout(config.hidden_dropout_prob)
        self.classifier = tf.keras.layers.Dense(config.num_labels)

    def call(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        training=False
    ):

        discriminator_hidden_states = self.discriminator(
            input_ids, attention_mask, token_type_ids, position_ids, head_mask, inputs_embeds, training=training
        )
        discriminator_sequence_output = discriminator_hidden_states[0]
        discriminator_sequence_output = self.dropout(discriminator_sequence_output)
        logits = self.classifier(discriminator_sequence_output)
        output = (logits,)
        output += discriminator_hidden_states[1:]

        return output  # (loss), scores, (hidden_states), (attentions)
