import time
import torch
import datetime

from torch.cuda import max_memory_allocated
from torch.nn.utils.clip_grad import clip_grad_norm_

from utils.misc import get_grad_norm


class Trainer:
    @classmethod
    def train(cls, accelerator, model, dataloader, optimizer, lr_scheduler, metric_computor,
              config, logger, epoch, progress_bar, is_regression=False, pruner=None, teacher=None, 
              kd_cls_loss=None, kd_reg_loss=None):
        model.train()

        batch_loss, step_lr = [], []
        start = batch_start = time.time()
        for step, batch in enumerate(dataloader):
            # Kd
            if teacher is not None:
                assert kd_cls_loss is not None and kd_reg_loss is not None, \
                    "'kd_cls_loss' & 'kd_reg_loss' must be set"

                # Forward
                outputs = model(**batch, output_attentions=True, output_hidden_states=True)
                # hidden_states, attns = outputs.hidden_states, outputs.attentions
                hidden_states, attns, logits = outputs.hidden_states, outputs.attentions, outputs.logits

                # Pay attention to set 'no_grad' for teacher forwarding
                with torch.no_grad():
                    teacher_outputs = teacher(**batch, output_attentions=True, output_hidden_states=True)
                    # teacher_hidden_states, teacher_attns = \
                    #     teacher_outputs.hidden_states, teacher_outputs.attentions
                    teacher_hidden_states, teacher_attns, teacher_logits = teacher_outputs.hidden_states, \
                        teacher_outputs.attentions, teacher_outputs.logits

                # Logits loss(ce)
                logit_loss = kd_cls_loss(logits, teacher_logits)

                # Hidden states loss(mse)
                hs_loss = 0.
                for layer_hidden_state, teacher_layer_hidden_state in \
                    zip(hidden_states[config.TRAIN.KD.BEGIN_LAYER:], teacher_hidden_states[config.TRAIN.KD.BEGIN_LAYER:]):
                    hs_loss = hs_loss + kd_reg_loss(layer_hidden_state, teacher_layer_hidden_state)

                # Attentions loss(mse)
                attn_loss = 0.
                for layer_attn, teacher_layer_attn in \
                    zip(attns[config.TRAIN.KD.BEGIN_LAYER:], teacher_attns[config.TRAIN.KD.BEGIN_LAYER:]):
                    attn_loss = attn_loss + kd_reg_loss(layer_attn, teacher_layer_attn)
                
                # TODO: verify this weighted loss
                # loss_raw = 2 * logit_loss + hs_loss + attn_loss
                loss_raw = logit_loss + hs_loss + attn_loss
            else:
                outputs = model(**batch)
                loss_raw = outputs.loss
            
            # Gradient accumulation
            loss = loss_raw / config.TRAIN.GRADIENT_ACCUMULATION_STEPS
            batch_loss.append(loss.item())
            
            # Backward
            accelerator.backward(loss)

            # Clip gradient(optional)
            if config.TRAIN.CLIP_GRAD:
                grad_norm = clip_grad_norm_(
                    accelerator.unwrap_model(model).parameters(), max_norm=config.TRAIN.CLIP_GRAD)
            else:
                grad_norm = get_grad_norm(accelerator.unwrap_model(model).parameters())
            
            lr = optimizer.param_groups[0]['lr']
            step_lr.append(lr)

            # Update parameters, lr, zero gradients, pruning(optional)
            if not (step + 1) % config.TRAIN.GRADIENT_ACCUMULATION_STEPS or step == len(dataloader) - 1:
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()

                # Default update step is 1
                progress_bar.update()

                if pruner is not None:
                    # This is for old pruner
                    # pruner.prune()
                    cur_sparsity = pruner.prune()
                    if cur_sparsity is not None:
                        logger.info(f"=> current sparsity: {cur_sparsity}")

                    if pruner._update_mask_conditions():
                        # This is for old pruner
                        # layer_sparse_rate, total_sparse_rate = pruner.prune_sparsity()
                        layer_sparse_rate, total_sparse_rate = pruner.sparsity()
                        logger.info(f'\nweight sparsity: {total_sparse_rate}\n'
                                    f'layer weight sparsity:\n{layer_sparse_rate}\n')
                    
                    # pruner.prune()
            
            # For eavaluation
            predictions = outputs.logits.argmax(dim=-1) \
                if not is_regression else outputs.logits.squeeze()
            metric_computor.add_batch(
                predictions=accelerator.gather(predictions),
                references=accelerator.gather(batch["labels"]),
            )
            
            # If un-comment below, it is the max batch time overall processes
            # accelerator.wait_for_everyone()
            batch_time = time.time() - batch_start

            if not step % config.PRINT_FREQ:
                memory_used = max_memory_allocated() / (1024. ** 2)
                kd_loss_info = '' if teacher is None else \
                    (f"kd logit loss: {logit_loss.item():.8f}\t"
                     f"kd hidden states loss: {hs_loss.item():.8f}\t"
                     f"kd attentions loss: {attn_loss.item():.8f}")

                logger.info(
                    f'Train Epoch[{epoch}/{config.TRAIN.EPOCHS}] Step[{step}/{len(dataloader)}]\t'
                    f'lr: {lr:.10f}\t'
                    f'batch time: {batch_time:.2f}s\t'
                    f'loss raw: {loss_raw.item():.8f}\t'
                    f'loss(w gradient accumulate): {loss.item():.8f}\t'
                    f'{kd_loss_info}\t'
                    f'grad norm: {grad_norm:.8f}\t'
                    f'memory used: {memory_used:.0f}MB\n'
                )
            
            del loss
            if teacher is not None:
                # del hs_loss, attn_loss
                del logit_loss, hs_loss, attn_loss
            
            batch_start = time.time()
        epoch_time = time.time() - start

        train_loss = sum(batch_loss) / len(batch_loss)
        train_results = metric_computor.compute()
        logger.info(f"\n  Epoch{epoch} train loss: {train_loss:.6f} train metric: {train_results} training takes time: {datetime.timedelta(seconds=epoch_time)}\n")

        torch.cuda.empty_cache()

        return train_loss, train_results, step_lr


    @classmethod
    def val(cls, accelerator, model, dataloader, config, logger, 
            epoch, metric_computor, is_regression, teacher_mode=False):
        model.eval()
        
        batch_loss = []
        start = batch_start = time.time()
        for step, batch in enumerate(dataloader):
            with torch.no_grad():
                # loss, logits, hidden_states, attentions
                outputs = model(**batch)
                loss = outputs.loss
                batch_loss.append(loss.item())

                predictions = outputs.logits.argmax(dim=-1) \
                    if not is_regression else outputs.logits.squeeze()
                metric_computor.add_batch(
                    predictions=accelerator.gather(predictions),
                    references=accelerator.gather(batch["labels"]),
                )

            # If un-comment below, it is the max batch time overall processes
            # accelerator.wait_for_everyone()
            batch_time = time.time() - batch_start

            if not step % config.PRINT_FREQ:
                memory_used = max_memory_allocated() / (1024. ** 2)
                logger.info(
                    f'\n{"[Teacher]" if teacher_mode else ""}  '
                    f'Val Epoch[{epoch}/{config.TRAIN.EPOCHS}] Step[{step}/{len(dataloader)}]\t'
                    f'batch time: {batch_time:.2f}s\t'
                    f'loss: {loss.item():.8f}\t'
                    f'memory used: {memory_used:.0f}MB\n'
                )

            batch_start = time.time()
        epoch_time = time.time() - start

        val_loss = sum(batch_loss) / len(batch_loss)
        val_results = metric_computor.compute()
        logger.info(
            f"\n{'[Teacher]' if teacher_mode else ''}  "
            f"Epoch{epoch} val loss: {val_loss:.6f} val metric: {val_results} "
            f"validation takes time: {datetime.timedelta(seconds=epoch_time)}\t"
        )

        torch.cuda.empty_cache()
        
        return val_loss, val_results
