{
    "dataset_reader":{
        "type":"dureader_multi_passage_limited",
        "token_indexers":{
            "tokens":{
                "type":"single_id",
                "lowercase_tokens":true
            }
            ,
            "token_characters":{
                "type":"characters",
                "min_padding_length":5
            }
        },
        "lazy": true,
        "max_p_num": 5,
        "max_p_len": 400,
        "max_q_len": 60,
        // "max_samples": 1000,
    },
    "vocabulary":{
        "directory_path":"/data/nfsdata/meijie/data/dureader/vocabulary/",
    },
    "train_data_path":"/data/nfsdata/meijie/data/dureader/preprocessed/trainset/train.json",
    "validation_data_path":"/data/nfsdata/meijie/data/dureader/preprocessed/devset/dev.json",
    "model":{
        "type":"vnet",
        "text_field_embedder":{
            "type": "basic_with_loss",
            "token_embedders":{
                "tokens":{
                    "type":"embedding",
                    "pretrained_file":"/data/nfsdata/nlp/embeddings/chinese/tencent/Tencent_AILab_ChineseEmbedding.txt",
                    "embedding_dim":200,
                    "trainable":false
                },
                "token_characters":{
                    "type":"character_encoding",
                    "embedding":{
                        "num_embeddings":4100,
                        "embedding_dim":32
                    },
                    "encoder":{
                        "type":"cnn",
                        "embedding_dim":32,
                        "num_filters":32,
                        "ngram_filter_sizes":[
                            5
                        ]
                    },
                    "dropout":0.0
                }
                // ,
                // "token_characters":{
                //     "type":"glyph_encoder",
                //     "glyph_embsize": 128,
                //     "output_size": 128,
                //     "use_batch_norm": true,
                //     "encoder":{
                //         "type":"cnn",
                //         "embedding_dim":128,
                //         "num_filters":100,
                //         "ngram_filter_sizes":[
                //             1
                //         ]
                //     },
                //     "dropout":0.0
                // }
            }
        },
        "highway_embedding_size":182,
        "num_highway_layers":1,
        "phrase_layer":{
            "type":"lstm",
            "bidirectional":true,
            "input_size":182,
            "hidden_size":64,
            "num_layers":1,
            // "dropout":0.0
        },
        "modeling_layer":{
            "type":"lstm",
            "bidirectional":true,
            "input_size":512,
            "hidden_size":64,
            "num_layers":2,
            "dropout":0.0
        },
        "matrix_attention_layer": {
            "type": "linear",
            "tensor_1_dim": 182,
            "tensor_2_dim": 182,
            "combination": "x,y,x*y"
        },
        "pointer_net": {
            "bidirectional": false,
            "input_size": 640,
            "hidden_dim": 64,
            "lstm_layers": 2,
            "dropout": 0.0
        },
        "span_end_lstm":{
            "type":"lstm",
            "bidirectional":false,
            "input_size":640,
            "hidden_size":64,
            "num_layers":2,
            "dropout":0.0
        },
        "max_passage_len": 400,
        "ptr_dim":64,
        "max_num_passages": 5,
        "max_num_character": 15,
        "language": "zh",
        "dropout":0.0
    },
    "iterator":{
        "type":"bucket",
        "sorting_keys":[["question", "num_tokens"]],
        "biggest_batch_first":true,
        "batch_size": 32
    },
    "trainer":{
        "moving_average": {
            "type":"exponential",
            "decay": 0.99999
        },
        "num_epochs":10,
        "grad_clipping":true,
        "grad_norm":5,
        "patience":10,
        "validation_metric":"+rouge_L",
        "cuda_device":1,
        "learning_rate_scheduler":{
            "type":"reduce_on_plateau",
            "factor":0.5,
            "mode":"max",
            "patience":4
        },
        "optimizer":{
            "type":"adam",
            "betas":[
                0.8,
                0.9999
            ],
            "lr": 0.001
        }
    }
}
