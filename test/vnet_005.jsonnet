{
    "dataset_reader":{
        "type":"msmarco_multi_passage_limited",
        "token_indexers":{
            "tokens":{
                "type":"single_id",
                "lowercase_tokens":true
            },
            "token_characters":{
                "type":"characters"
            }
        },
        "passage_length_limit":400,
        "question_length_limit":50
    },
    "train_data_path":"/home/meefly/working/vnet/fixtures/small_samples.json",
    "validation_data_path":"/home/meefly/working/vnet/fixtures/small_samples.json",
    "model":{
        "type":"vnet",
        "text_field_embedder":{
            "token_embedders":{
                "tokens":{
                    "type":"embedding",
                    "pretrained_file":"/data/nfsdata/meijie/data/WordEmb/glove.6B.50d.txt",
                    "embedding_dim":50,
                    "trainable":true
                },
                "token_characters":{
                    "type":"character_encoding",
                    "embedding":{
                        "num_embeddings":262,
                        "embedding_dim":16
                    },
                    "encoder":{
                        "type":"cnn",
                        "embedding_dim":16,
                        "num_filters":100,
                        "ngram_filter_sizes":[
                            3
                        ]
                    },
                    "dropout":0.2
                }
            }
        },
        "num_highway_layers":2,
        "phrase_layer":{
            "type":"lstm",
            "bidirectional":true,
            "input_size":150,
            "hidden_size":100,
            "num_layers":2,
            "dropout":0.2
        },
        "match_layer":{
            "type":"lstm",
            "bidirectional":true,
            "input_size":200,
            "hidden_size":100,
            "num_layers":2,
            "dropout":0.2
        },
        "modeling_layer":{
            "type":"lstm",
            "bidirectional":true,
            "input_size":800,
            "hidden_size":100,
            "num_layers":2,
            "dropout":0.2
        },
        "matrix_attention_layer": {
            "type": "linear",
            "tensor_1_dim": 150,
            "tensor_2_dim": 150,
            "combination": "x,y,x*y"
        },
        "span_end_lstm":{
            "type":"lstm",
            "bidirectional":false,
            "input_size":200,
            "hidden_size":100,
            "num_layers":2,
            "dropout":0.2
        },
        "span_end_encoder":{
            "type":"lstm",
            "bidirectional":true,
            "input_size":1400,
            "hidden_size":100,
            "num_layers":2,
            "dropout":0.2
        },
        "ptr_dim":200,
        "dropout":0.2
    },
    "iterator":{
        "type":"bucket",
        "sorting_keys":[ ["question", "num_tokens"]],
        "batch_size":10
    },
    "trainer":{
        "num_epochs":10,
        "grad_norm":5,
        "patience":10,
        "validation_metric":"+rouge_L",
        "cuda_device":-1,
        "learning_rate_scheduler":{
            "type":"reduce_on_plateau",
            "factor":0.5,
            "mode":"max",
            "patience":2
        },
        "optimizer":{
            "type":"adam",
            "betas":[
                0.9,
                0.9
            ]
        }
    }
}
