python -m tensorshake.translate.translate_v2 \
       --num_layers 2 \
       --size 256 \
       --steps_per_checkpoint 50 \
       --learning_rate 0.01 \
       --learning_rate_decay_factor 0.9 \
       --train_dir /cache \
       --decode 1 # turn on for prediction
