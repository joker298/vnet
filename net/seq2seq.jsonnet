{
  "dataset_reader": {
    "type": "seq2seq",
    "source_tokenizer": {
      "type": "word",
      "word_splitter": {
        "type": "spacy",
        "pos_tags": true,
        "parse": true,
        "ner": true
      }
    },
    "target_tokenizer": {
      "type": "word"
    },
    "source_token_indexers": {
      "tokens": {
        "type": "single_id",
        "namespace": "source_tokens"
      },
      "pos_tags": {
        "type": "pos_tag",
        "namespace": "pos"
      },
      "dependency_label": {
        "type": "dependency_label",
        "namespace": "dependencies"
      },
      "ner_tags": {
        "type": "ner_tag",
        "namespace": "ner"
      }
    },
    "target_token_indexers": {
      "tokens": {
        "namespace": "target_tokens"
      }
    }
  },
  "train_data_path": "/home/meefly/data/vnet_seq2seq/train.tsv",
  "validation_data_path": "/home/meefly/data/vnet_seq2seq/dev.tsv",
  "model": {
    "type": "simple_seq2seq",
    "source_embedder": {
      "token_embedders": {
        "tokens": {
          "type": "embedding",
          "vocab_namespace": "source_tokens",
          "embedding_dim": 25,
          "trainable": true
        },
        "pos_tags": {
          "type": "embedding",
          "vocab_namespace": "pos",
          "embedding_dim": 5
        },
        "ner_tags": {
          "type": "embedding",
          "vocab_namespace": "ner",
          "embedding_dim": 7
        },
        "dependency_label": {
          "type": "embedding",
          "vocab_namespace": "dependencies",
          "embedding_dim": 10
        }
      }
    },
    "encoder": {
      "type": "lstm",
      "input_size": 47,
      "hidden_size": 10,
      "num_layers": 1
    },
    "max_decoding_steps": 20,
    "target_embedding_dim": 30,
    "target_namespace": "target_tokens",
    "attention": {
      "type": "dot_product"
    },
    "beam_size": 5
  },
  "iterator": {
    "type": "bucket",
    "padding_noise": 0.0,
    "batch_size" : 80,
    "sorting_keys": [["source_tokens", "num_tokens"]]
  },
  "trainer": {
    "num_epochs": 2,
    "patience": 10,
    "cuda_device": 0,
    "optimizer": {
      "type": "adam",
      "lr": 0.01
    }
  }
}